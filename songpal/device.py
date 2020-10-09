"""Module presenting a single supported device."""
import asyncio
import itertools
import logging
from collections import defaultdict
from pprint import pformat as pf
from typing import Any, Dict, List
from urllib.parse import urlparse

import aiohttp
from async_upnp_client import UpnpFactory
from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.profiles.dlna import DmrDevice

from didl_lite import didl_lite
from songpal.common import ProtocolType, SongpalConnectionException, SongpalException
from songpal.containers import (
    Content,
    ContentInfo,
    Input,
    InterfaceInfo,
    PlayInfo,
    Power,
    Scheme,
    Setting,
    SettingsEntry,
    SoftwareUpdateInfo,
    Source,
    Storage,
    SupportedFunctions,
    Sysinfo,
    Volume,
    Zone,
)
from songpal.discovery import Discover
from songpal.notification import ConnectChange, Notification
from songpal.service import Service
from wakeonlan import send_magic_packet

_LOGGER = logging.getLogger(__name__)


class Device:
    """This is the main entry point for communicating with a device.

    In order to use this you need to obtain the URL for the API first.
    """

    WEBSOCKET_PROTOCOL = "v10.webapi.scalar.sony.com"
    WEBSOCKET_VERSION = 13

    def __init__(self, endpoint, force_protocol=None, debug=0):
        """Initialize Device.

        :param endpoint: the main API endpoint.
        :param force_protocol: can be used to force the protocol (xhrpost/websocket).
        :param debug: debug level. larger than 1 gives even more debug output.
        """
        self.debug = debug
        endpoint = urlparse(endpoint)
        self.endpoint = endpoint.geturl()
        _LOGGER.debug("Endpoint: %s" % self.endpoint)

        self.guide_endpoint = endpoint._replace(path="/sony/guide").geturl()
        _LOGGER.debug("Guide endpoint: %s" % self.guide_endpoint)

        if force_protocol:
            _LOGGER.warning("Forcing protocol %s", force_protocol)
        self.force_protocol = force_protocol

        self.idgen = itertools.count(start=1)
        self.services = {}  # type: Dict[str, Service]

        self.callbacks = defaultdict(set)

        self._sysinfo = None

        self._upnp_discovery = None
        self._upnp_device = None
        self._upnp_renderer = None

    async def __aenter__(self):
        """Asynchronous context manager, initializes the list of available methods."""
        await self.get_supported_methods()

    async def create_post_request(self, method: str, params: Dict = None):
        """Call the given method over POST.

        :param method: Name of the method
        :param params: dict of parameters
        :return: JSON object
        """
        if params is None:
            params = {}
        headers = {"Content-Type": "application/json"}
        payload = {
            "method": method,
            "params": [params],
            "id": next(self.idgen),
            "version": "1.0",
        }

        if self.debug > 1:
            _LOGGER.debug("> POST %s with body: %s", self.guide_endpoint, payload)

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                res = await session.post(
                    self.guide_endpoint, json=payload, headers=headers
                )
                if self.debug > 1:
                    _LOGGER.debug("Received %s: %s" % (res.status, res.text))
                if res.status != 200:
                    res_json = await res.json(content_type=None)
                    raise SongpalException(
                        "Got a non-ok (status %s) response for %s"
                        % (res.status, method),
                        error=res_json.get("error"),
                    )

                res_json = await res.json(content_type=None)
        except aiohttp.ClientConnectionError as ex:
            raise SongpalConnectionException(ex)
        except aiohttp.InvalidURL as ex:
            raise SongpalException("Unable to do POST request: %s" % ex) from ex

        if "error" in res_json:
            raise SongpalException(
                "Got an error for %s" % method, error=res_json["error"]
            )

        if self.debug > 1:
            _LOGGER.debug("Got %s: %s", method, pf(res_json))

        return res_json

    async def request_supported_methods(self):
        """Return JSON formatted supported API."""
        return await self.create_post_request("getSupportedApiInfo")

    async def get_supported_methods(self):
        """Get information about supported methods.

        Calling this as the first thing before doing anything else is
        necessary to fill the available services table.
        """
        try:
            response = await self.request_supported_methods()

            if "result" in response:
                services = response["result"][0]
                _LOGGER.debug("Got %s services!" % len(services))

                for x in services:
                    serv = await Service.from_payload(
                        x, self.endpoint, self.idgen, self.debug, self.force_protocol
                    )
                    if serv is not None:
                        self.services[x["service"]] = serv
                    else:
                        _LOGGER.warning("Unable to create service %s", x["service"])

                for service in self.services.values():
                    if self.debug > 1:
                        _LOGGER.debug("Service %s", service)
                    for api in service.methods:
                        # self.logger.debug("%s > %s" % (service, api))
                        if self.debug > 1:
                            _LOGGER.debug("> %s" % api)
                return self.services

            return None
        except SongpalException as e:
            found_services = None
            if e.code == 12 and e.error_message == "getSupportedApiInfo":
                found_services = await self._get_supported_methods_upnp()

            if found_services:
                return found_services
            else:
                raise e

    async def _get_supported_methods_upnp(self):
        if self._upnp_discovery:
            return self.services

        host = urlparse(self.endpoint).hostname

        async def find_device(device):
            if host == urlparse(device.endpoint).hostname:
                self._upnp_discovery = device

        await Discover.discover(1, self.debug, callback=find_device)

        if self._upnp_discovery is None:
            return None

        for service_name in self._upnp_discovery.services:
            service = Service(
                service_name,
                self.endpoint + "/" + service_name,
                ProtocolType.XHRPost,
                self.idgen,
            )
            await service.fetch_methods(self.debug)
            self.services[service_name] = service

        return self.services

    async def get_power(self) -> Power:
        """Get the device state."""
        if not self.services:
            # We could not retrieve services, device is offline
            return Power.make(status=False)

        res = await self.services["system"]["getPowerStatus"]()
        return Power.make(**res)

    async def set_power(self, value: bool, wol: List[str] = None, get_sys_info=False):
        """Toggle the device on and off."""
        if value:
            status = "active"
        else:
            status = "off"

        if value is False and get_sys_info is True and self._sysinfo is None:
            # get sys info to be able to turn device back on
            try:
                await self.get_system_info()
            except Exception:
                pass

        try:
            if "system" in self.services:
                return await self.services["system"]["setPowerStatus"](status=status)
            else:
                raise SongpalException("System service not available")
        except SongpalException as e:
            if value and (self._sysinfo or wol):
                if wol:
                    logging.debug(
                        "Sending WoL magic packet to supplied mac addresses %s", wol
                    )
                    send_magic_packet(*wol)
                    return
                if self._sysinfo:
                    logging.debug(
                        "Sending WoL magic to known mac addresses %s",
                        (self._sysinfo.macAddr, self._sysinfo.wirelessMacAddr),
                    )
                    send_magic_packet(
                        *[
                            mac
                            for mac in (
                                self._sysinfo.macAddr,
                                self._sysinfo.wirelessMacAddr,
                            )
                            if mac is not None
                        ]
                    )
                    return
            raise e

    async def get_play_info(self) -> PlayInfo:
        """Return  of the device."""
        info = await self.services["avContent"]["getPlayingContentInfo"]({})
        return PlayInfo.make(**info.pop())

    async def get_power_settings(self) -> List[Setting]:
        """Get power settings."""
        return [
            Setting.make(**x)
            for x in await self.services["system"]["getPowerSettings"]({})
        ]

    async def set_power_settings(self, target: str, value: str) -> None:
        """Set power settings."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["system"]["setPowerSettings"](params)

    async def get_googlecast_settings(self) -> List[Setting]:
        """Get Googlecast settings."""
        return [
            Setting.make(**x)
            for x in await self.services["system"]["getWuTangInfo"]({})
        ]

    async def set_googlecast_settings(self, target: str, value: str):
        """Set Googlecast settings."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["system"]["setWuTangInfo"](params)

    async def request_settings_tree(self):
        """Get raw settings tree JSON.

        Prefer :func:get_settings: for containerized settings.
        """
        settings = await self.services["system"]["getSettingsTree"](usage="")
        return settings

    async def get_settings(self) -> List[SettingsEntry]:
        """Get a list of available settings.

        See :func:request_settings_tree: for raw settings.
        """
        settings = await self.request_settings_tree()
        return [SettingsEntry.make(**x) for x in settings["settings"]]

    async def get_misc_settings(self) -> List[Setting]:
        """Return miscellaneous settings such as name and timezone."""
        misc = await self.services["system"]["getDeviceMiscSettings"](target="")
        return [Setting.make(**x) for x in misc]

    async def set_misc_settings(self, target: str, value: str):
        """Change miscellaneous settings."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["system"]["setDeviceMiscSettings"](params)

    async def get_interface_information(self) -> InterfaceInfo:
        """Return generic product information."""
        iface = await self.services["system"]["getInterfaceInformation"]()
        return InterfaceInfo.make(**iface)

    async def get_system_info(self) -> Sysinfo:
        self._sysinfo = await self._get_system_info()
        return self._sysinfo

    async def _get_system_info(self) -> Sysinfo:
        """Return system information including mac addresses and current version."""

        if self.services["system"].has_method("getSystemInformation"):
            return Sysinfo.make(
                **await self.services["system"]["getSystemInformation"]()
            )
        elif self.services["system"].has_method("getNetworkSettings"):
            info = await self.services["system"]["getNetworkSettings"](netif="")

            def get_addr(info, iface):
                addr = next((i for i in info if i["netif"] == iface), {}).get("hwAddr")
                return addr.lower().replace("-", ":") if addr else addr

            macAddr = get_addr(info, "eth0")
            wirelessMacAddr = get_addr(info, "wlan0")
            version = self._upnp_discovery.version if self._upnp_discovery else None
            return Sysinfo.make(
                macAddr=macAddr, wirelessMacAddr=wirelessMacAddr, version=version
            )
        else:
            raise SongpalException("getSystemInformation not supported")

    async def get_sleep_timer_settings(self) -> List[Setting]:
        """Get sleep timer settings."""
        return [
            Setting.make(**x)
            for x in await self.services["system"]["getSleepTimerSettings"]({})
        ]

    async def get_storage_list(self) -> List[Storage]:
        """Return information about connected storage devices."""
        return [
            Storage.make(**x)
            for x in await self.services["system"]["getStorageList"]({})
        ]

    async def get_update_info(self, from_network=True) -> SoftwareUpdateInfo:
        """Get information about updates."""
        if from_network:
            from_network = "true"
        else:
            from_network = "false"
        # from_network = ""
        info = await self.services["system"]["getSWUpdateInfo"](network=from_network)
        return SoftwareUpdateInfo.make(**info)

    async def activate_system_update(self) -> None:
        """Start a system update if available."""
        return await self.services["system"]["actSWUpdate"]()

    async def get_inputs(self) -> List[Input]:
        """Return list of available outputs."""
        if "avContent" in self.services:
            res = await self.services["avContent"][
                "getCurrentExternalTerminalsStatus"
            ]()
            return [
                Input.make(services=self.services, **x)
                for x in res
                if "meta:zone:output" not in x["meta"]
            ]
        else:
            if self._upnp_discovery is None:
                raise SongpalException(
                    "avContent service not available and UPnP fallback failed"
                )

            return await self._get_inputs_upnp()

    async def _get_upnp_services(self):
        requester = AiohttpRequester()
        factory = UpnpFactory(requester)

        if self._upnp_device is None:
            self._upnp_device = await factory.async_create_device(
                self._upnp_discovery.upnp_location
            )

        if self._upnp_renderer is None:
            media_renderers = await DmrDevice.async_search(timeout=1)
            host = urlparse(self.endpoint).hostname
            media_renderer_location = next(
                (
                    r["location"]
                    for r in media_renderers
                    if urlparse(r["location"]).hostname == host
                ),
                None,
            )
            if media_renderer_location is None:
                raise SongpalException("Could not find UPnP media renderer")

            self._upnp_renderer = await factory.async_create_device(
                media_renderer_location
            )

    async def _get_inputs_upnp(self):
        await self._get_upnp_services()

        content_directory = self._upnp_device.service(
            next(
                s for s in self._upnp_discovery.upnp_services if "ContentDirectory" in s
            )
        )

        browse = content_directory.action("Browse")
        filter = (
            "av:BIVL,av:liveType,av:containerClass,dc:title,dc:date,"
            "res,res@duration,res@resolution,upnp:albumArtURI,"
            "upnp:albumArtURI@dlna:profileID,upnp:artist,upnp:album,upnp:genre"
        )
        result = await browse.async_call(
            ObjectID="0",
            BrowseFlag="BrowseDirectChildren",
            Filter=filter,
            StartingIndex=0,
            RequestedCount=25,
            SortCriteria="",
        )

        root_items = didl_lite.from_xml_string(result["Result"])
        input_item = next(
            (
                i
                for i in root_items
                if isinstance(i, didl_lite.Container) and i.title == "Input"
            ),
            None,
        )

        result = await browse.async_call(
            ObjectID=input_item.id,
            BrowseFlag="BrowseDirectChildren",
            Filter=filter,
            StartingIndex=0,
            RequestedCount=25,
            SortCriteria="",
        )

        av_transport = self._upnp_renderer.service(
            next(s for s in self._upnp_renderer.services if "AVTransport" in s)
        )

        media_info = await av_transport.action("GetMediaInfo").async_call(InstanceID=0)
        current_uri = media_info.get("CurrentURI")

        inputs = didl_lite.from_xml_string(result["Result"])
        return [
            Input.make(
                title=i.title,
                uri=i.resources[0].uri,
                active="active" if i.resources[0].uri in current_uri else "",
                avTransport=av_transport,
                uriMetadata=didl_lite.to_xml_string(i).decode("utf-8"),
            )
            for i in inputs
        ]

    async def get_zones(self) -> List[Zone]:
        """Return list of available zones."""
        res = await self.services["avContent"]["getCurrentExternalTerminalsStatus"]()
        zones = [
            Zone.make(services=self.services, **x)
            for x in res
            if "meta:zone:output" in x["meta"]
        ]
        if not zones:
            raise SongpalException("Device has no zones")
        return zones

    async def get_zone(self, name) -> Zone:
        zones = await self.get_zones()
        try:
            zone = next((x for x in zones if x.title == name))
            return zone
        except StopIteration:
            raise SongpalException("Unable to find zone %s" % name)

    async def get_setting(self, service: str, method: str, target: str):
        """Get a single setting for service.

        :param service: Service to query.
        :param method: Getter method for the setting, read from ApiMapping.
        :param target: Setting to query.
        :return: JSON response from the device.
        """
        return await self.services[service][method](target=target)

    async def get_bluetooth_settings(self) -> List[Setting]:
        """Get bluetooth settings."""
        bt = await self.services["avContent"]["getBluetoothSettings"]({})
        return [Setting.make(**x) for x in bt]

    async def set_bluetooth_settings(self, target: str, value: str) -> None:
        """Set bluetooth settings."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["avContent"]["setBluetoothSettings"](params)

    async def get_custom_eq(self):
        """Get custom EQ settings."""
        return await self.services["audio"]["getCustomEqualizerSettings"]({})

    async def set_custom_eq(self, target: str, value: str) -> None:
        """Set custom EQ settings."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["audio"]["setCustomEqualizerSettings"](params)

    async def get_supported_playback_functions(
        self, uri=""
    ) -> List[SupportedFunctions]:
        """Return list of inputs and their supported functions."""
        return [
            SupportedFunctions.make(**x)
            for x in await self.services["avContent"]["getSupportedPlaybackFunction"](
                uri=uri
            )
        ]

    async def get_playback_settings(self) -> List[Setting]:
        """Get playback settings such as shuffle and repeat."""
        return [
            Setting.make(**x)
            for x in await self.services["avContent"]["getPlaybackModeSettings"]({})
        ]

    async def set_playback_settings(self, target, value) -> None:
        """Set playback settings such a shuffle and repeat."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["avContent"]["setPlaybackModeSettings"](params)

    async def get_schemes(self) -> List[Scheme]:
        """Return supported uri schemes."""
        return [
            Scheme.make(**x)
            for x in await self.services["avContent"]["getSchemeList"]()
        ]

    async def get_source_list(self, scheme: str = "") -> List[Source]:
        """Return available sources for playback."""
        res = await self.services["avContent"]["getSourceList"](scheme=scheme)
        return [Source.make(**x) for x in res]

    async def get_content_count(self, source: str):
        """Return file listing for source."""
        params = {"uri": source, "type": None, "target": "all", "view": "flat"}
        return ContentInfo.make(
            **await self.services["avContent"]["getContentCount"](params)
        )

    async def get_contents(self, uri) -> List[Content]:
        """Request content listing recursively for the given URI.

        :param uri: URI for the source.
        :return: List of Content objects.
        """
        contents = [
            Content.make(**x)
            for x in await self.services["avContent"]["getContentList"](uri=uri)
        ]
        contentlist = []

        for content in contents:
            if content.contentKind == "directory" and content.index >= 0:
                # print("got directory %s" % content.uri)
                res = await self.get_contents(content.uri)
                contentlist.extend(res)
            else:
                contentlist.append(content)
                # print("%s%s" % (' ' * depth, content))
        return contentlist

    async def get_volume_information(self) -> List[Volume]:
        """Get the volume information."""
        if "audio" in self.services and self.services["audio"].has_method(
            "getVolumeInformation"
        ):
            res = await self.services["audio"]["getVolumeInformation"]({})
            volume_info = [Volume.make(services=self.services, **x) for x in res]
            if len(volume_info) < 1:
                logging.warning("Unable to get volume information")
            elif len(volume_info) > 1:
                logging.debug("The device seems to have more than one volume setting.")
            return volume_info
        else:
            return await self._get_volume_information_upnp()

    async def _get_volume_information_upnp(self):
        await self._get_upnp_services()

        rendering_control_service = self._upnp_renderer.service(
            next(s for s in self._upnp_renderer.services if "RenderingControl" in s)
        )
        volume_result = await rendering_control_service.action("GetVolume").async_call(
            InstanceID=0, Channel="Master"
        )
        mute_result = await rendering_control_service.action("GetMute").async_call(
            InstanceID=0, Channel="Master"
        )

        min_volume = rendering_control_service.state_variables["Volume"].min_value
        max_volume = rendering_control_service.state_variables["Volume"].max_value

        return [
            Volume.make(
                volume=volume_result["CurrentVolume"],
                mute=mute_result["CurrentMute"],
                minVolume=min_volume,
                maxVolume=max_volume,
                step=1,
                renderingControl=rendering_control_service,
            )
        ]

    async def get_sound_settings(self, target="") -> List[Setting]:
        """Get the current sound settings.

        :param str target: settings target, defaults to all.
        """
        res = await self.services["audio"]["getSoundSettings"]({"target": target})
        return [Setting.make(**x) for x in res]

    async def get_soundfield(self) -> List[Setting]:
        """Get the current sound field settings."""
        res = await self.services["audio"]["getSoundSettings"]({"target": "soundField"})
        return Setting.make(**res[0])

    async def set_soundfield(self, value):
        """Set soundfield."""
        return await self.set_sound_settings("soundField", value)

    async def set_sound_settings(self, target: str, value: str):
        """Change a sound setting."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["audio"]["setSoundSettings"](params)

    async def get_speaker_settings(self) -> List[Setting]:
        """Return speaker settings."""
        speaker_settings = await self.services["audio"]["getSpeakerSettings"]({})
        return [Setting.make(**x) for x in speaker_settings]

    async def set_speaker_settings(self, target: str, value: str):
        """Set speaker settings."""
        params = {"settings": [{"target": target, "value": value}]}
        return await self.services["audio"]["setSpeakerSettings"](params)

    async def get_available_playback_functions(self, output=""):
        """Return available playback functions.

        If no output is given the current is assumed.
        """
        await self.services["avContent"]["getAvailablePlaybackFunction"](output=output)

    def on_notification(self, type_, callback):
        """Register a notification callback.

        The callbacks registered by this method are called when an expected
        notification is received from the device.
        To listen for notifications call :func:listen_notifications:.
        :param type_: Type of the change, e.g., VolumeChange or PowerChange
        :param callback: Callback to call when a notification is received.
        :return:
        """
        self.callbacks[type_].add(callback)

    def clear_notification_callbacks(self):
        """Clear all notification callbacks."""
        self.callbacks.clear()

    async def listen_notifications(self, fallback_callback=None):
        """Listen for notifications from the device forever.

        Use :func:on_notification: to register what notifications to listen to.
        """
        tasks = []

        async def handle_notification(notification):
            if type(notification) not in self.callbacks:
                if not fallback_callback:
                    _LOGGER.debug("No callbacks for %s", notification)
                    # _LOGGER.debug("Existing callbacks for: %s" % self.callbacks)
                else:
                    await fallback_callback(notification)
                return
            for cb in self.callbacks[type(notification)]:
                await cb(notification)

        for serv in self.services.values():
            tasks.append(
                asyncio.ensure_future(
                    serv.listen_all_notifications(handle_notification)
                )
            )

        try:
            print(await asyncio.gather(*tasks))
        except Exception as ex:
            # TODO: do a slightly restricted exception handling?
            # Notify about disconnect
            await handle_notification(ConnectChange(connected=False, exception=ex))
            return

    async def stop_listen_notifications(self):
        """Stop listening on notifications."""
        _LOGGER.debug("Stopping listening for notifications..")
        for serv in self.services.values():
            await serv.stop_listen_notifications()

        return True

    async def get_notifications(self) -> List[Notification]:
        """Get available notifications, which can then be subscribed to.

        Call :func:activate: to enable notifications, and :func:listen_notifications:
        to loop forever for notifications.

        :return: List of Notification objects
        """
        notifications = []
        for serv in self.services:
            for notification in self.services[serv].notifications:
                notifications.append(notification)
        return notifications

    async def raw_command(self, service: str, method: str, params: Any):
        """Call an arbitrary method with given parameters.

        This is useful for debugging and trying out commands before
        implementing them properly.
        :param service: Service, use list(self.services) to get a list of availables.
        :param method: Method to call.
        :param params: Parameters as a python object (e.g., dict, list)
        :return: Raw JSON response from the device.
        """
        _LOGGER.info("Calling %s.%s(%s)", service, method, params)
        return await self.services[service][method](params)
