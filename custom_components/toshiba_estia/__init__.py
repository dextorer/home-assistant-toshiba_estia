"""The Toshiba AC integration."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from toshiba_estia.device_manager import ToshibaAcDeviceManager
from toshiba_estia.utils.http_api import ToshibaAcHttpApi

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN

# Since ~2026-07-16 Toshiba's WAF returns 429 on /api/Consumer/Login unless a
# Device-ID header (any 16-hex value) is present. The pinned toshiba-estia
# library (v0.1.2) doesn't send one, so inject it as an aiohttp session
# default until upstream ships a fix.
# See https://github.com/h4de5/home-assistant-toshiba_ac/issues/297
_WAF_DEVICE_ID = "a1b2c3d4e5f60718"

_original_http_connect = ToshibaAcHttpApi.connect


async def _http_connect_with_device_id(self):
    if not self.session:
        self.session = aiohttp.ClientSession(headers={"Device-ID": _WAF_DEVICE_ID})
    await _original_http_connect(self)


ToshibaAcHttpApi.connect = _http_connect_with_device_id

PLATFORMS = ["climate",  "sensor",  "water_heater", "binary_sensor"]

SETUP_TIMEOUT = 60  # seconds — Azure IoT MQTT negotiation needs time at startup

_LOGGER = logging.getLogger(__name__)


async def sas_token_updated_for_entry(
    hass: HomeAssistant, entry: ConfigEntry, new_sas_token: str
):
    """Update SAS token."""
    _LOGGER.info("SAS token updated")

    new_data = {**entry.data, "sas_token": new_sas_token}
    hass.config_entries.async_update_entry(entry, data=new_data)


def add_sas_token_updated_callback_for_entry(
    hass: HomeAssistant, entry: ConfigEntry, device_manager: ToshibaAcDeviceManager
):
    """Set up SAS token update callback."""

    async def wrapper_callback(new_sas_token: str):
        await sas_token_updated_for_entry(hass, entry, new_sas_token)

    device_manager.on_sas_token_updated_callback.add(wrapper_callback)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Toshiba Estia component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Toshiba AC from a config entry."""
    device_manager = ToshibaAcDeviceManager(
        entry.data["username"],
        entry.data["password"],
        entry.data["device_id"],
        entry.data["sas_token"],
    )

    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            await device_manager.connect()
    except Exception as ex:
        _LOGGER.warning("Initial connection failed: %s. Trying new sas_token...", ex)
        try:
            await device_manager.shutdown()
        except Exception:
            pass
        device_manager = ToshibaAcDeviceManager(
            entry.data["username"], entry.data["password"], entry.data["device_id"]
        )
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                new_sas_token = await device_manager.connect()
            new_data = {**entry.data, "sas_token": new_sas_token}
            hass.config_entries.async_update_entry(entry, data=new_data)
        except Exception as ex2:
            try:
                await device_manager.shutdown()
            except Exception:
                pass
            raise ConfigEntryNotReady(
                "Toshiba cloud not reachable, will retry"
            ) from ex2

    # Pre-fetch devices so platform setup doesn't make additional cloud calls.
    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            await device_manager.get_devices()
    except Exception as ex:
        try:
            await device_manager.shutdown()
        except Exception:
            pass
        raise ConfigEntryNotReady(
            "Failed to fetch devices, will retry"
        ) from ex

    add_sas_token_updated_callback_for_entry(hass, entry, device_manager)

    hass.data[DOMAIN][entry.entry_id] = device_manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.error("Unload Toshiba integration")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        device_manager: ToshibaAcDeviceManager = hass.data[DOMAIN][entry.entry_id]
        try:
            await device_manager.shutdown()
        except Exception as ex:
            _LOGGER.error("Error while unloading Toshiba integration %s", ex)
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
