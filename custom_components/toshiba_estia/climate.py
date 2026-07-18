"""Platform for climate integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from time import monotonic
from typing import Any

from toshiba_estia.device import ToshibaAcDevice

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .const import DOMAIN
from .entity import ToshibaAcStateEntity
from .estia_compat import set_hvac_mode, set_zone_temperature, set_zones_enabled

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_devices):
    """Add climate for passed config_entry in HA."""
    device_manager = hass.data[DOMAIN][config_entry.entry_id]
    new_entities = []

    _LOGGER.info("Registering climate entries")

    devices = await device_manager.get_devices()
    for device in devices:
        climate_zone1 = ToshibaHeatingZone(device, zone=1)
        new_entities.append(climate_zone1)

    if new_entities:
        _LOGGER.info("Adding %d %s", len(new_entities), "climates")
        async_add_devices(new_entities)


class ToshibaHeatingZone(ToshibaAcStateEntity, ClimateEntity):
    """Provides a Toshiba climates."""

    # This is the main entity for the device
    _attr_has_entity_name = True
    _attr_name = None

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.AUTO,
    ]
    _attr_target_temperature_step = 1
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, toshiba_device: ToshibaAcDevice, zone: int = 1):
        """Initialize the climate."""
        super().__init__(toshiba_device)

        self._enable_turn_on_off_backwards_compatibility = False
        self.zone = zone
        self._attr_unique_id = f"{self._device.ac_unique_id}_climate_zone{zone}"
        self._attr_name = f"Zone {zone}"
        self._zone_power_override: bool | None = None
        self._zone_power_override_ts = 0.0

    def _raw_byte(self, one_based_index: int) -> int | None:
        raw = getattr(self._device.fcu_state, "_status_string", "")
        start = (one_based_index - 1) * 2
        end = start + 2
        if len(raw) < end:
            return None
        try:
            return int(raw[start:end], 16)
        except ValueError:
            return None

    def _is_water_function_active(self) -> bool:
        # Byte 5 uses explicit codes: 0x03=enabled, 0x02=disabled.
        return self._raw_byte(5) == 0x03

    def _raw_water_mode(self) -> int | None:
        # Byte 6: operation mode (0x05 cool, 0x06 heat).
        return self._raw_byte(6)

    def _is_zone_power_enabled(self) -> bool:
        raw_active = self._is_water_function_active()
        if not raw_active:
            # Raw OFF from Estia always wins over local optimistic state.
            self._zone_power_override = None
            return False
        if self._zone_power_override is not None:
            # Keep OFF/ON UX stable while telemetry catches up.
            if monotonic() - self._zone_power_override_ts < 180:
                return self._zone_power_override
            self._zone_power_override = None
        return raw_active


    @property
    def is_on(self):
        """Return True if the device is on or completely off."""
        return self._is_zone_power_enabled()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        set_temperature = kwargs[ATTR_TEMPERATURE]
        await set_zone_temperature(self._device, self.zone, int(set_temperature))

    async def async_turn_on(self) -> None:
        """Turn device on."""
        self._zone_power_override = True
        self._zone_power_override_ts = monotonic()
        await set_zones_enabled(self._device, True)

    async def async_turn_off(self) -> None:
        """Turn device off."""
        self._zone_power_override = False
        self._zone_power_override_ts = monotonic()
        await set_zones_enabled(self._device, False)

    async def async_toggle(self) -> None:
        """Toggle device status."""
        if not self.is_on:
            await self.async_turn_on()
        else:
            await self.async_turn_off()

    @property
    def hvac_mode(self) -> HVACMode | str | None:
        """Return hvac operation ie. heat, cool mode."""
        if not self.is_on:
            return HVACMode.OFF
        mode_raw = self._raw_water_mode()
        if mode_raw == 0x05:
            return HVACMode.COOL
        if mode_raw == 0x06:
            return HVACMode.HEAT
        return HVACMode.AUTO

    @property
    def hvac_modes(self) -> list[HVACMode] | list[str]:
        """Return the list of available hvac operation modes."""
        return self._attr_hvac_modes

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        _LOGGER.info("Toshiba Climate setting hvac_mode: %s", hvac_mode)

        if hvac_mode == HVACMode.OFF:
            self._zone_power_override = False
            self._zone_power_override_ts = monotonic()
            await set_zones_enabled(self._device, False)
        else:
            self._zone_power_override = True
            self._zone_power_override_ts = monotonic()
            await set_hvac_mode(self._device, hvac_mode)


    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        # Estia does not expose a reliable room temperature per zone.
        # Returning target avoids HomeKit's default 21.0°C fallback.
        return self.target_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._device.zone1_target_temperature

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 5

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 40

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return entity specific state attributes."""
        return {
            "outdoor_temperature": self._device.temperatures.to,
        }
