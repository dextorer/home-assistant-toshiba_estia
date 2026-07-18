"""Platform for water heater integration."""
from __future__ import annotations

import logging
from time import monotonic

from toshiba_estia.device import ToshibaAcDevice

from homeassistant.components.water_heater import WaterHeaterEntity, WaterHeaterEntityFeature
from homeassistant.components.water_heater.const import (
    STATE_ELECTRIC,
    STATE_HEAT_PUMP,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .const import DOMAIN
from .entity import ToshibaAcStateEntity
from .estia_compat import set_dhw_operation_mode, set_dhw_temperature

_LOGGER = logging.getLogger(__name__)
STATE_OFF = "off"


async def async_setup_entry(hass, config_entry, async_add_devices):
    """Add water heater for passed config_entry in HA."""
    device_manager = hass.data[DOMAIN][config_entry.entry_id]
    new_entities = []

    _LOGGER.info("Registering water heater entries")

    devices = await device_manager.get_devices()
    for device in devices:
        new_entities.append(ToshibaDHW(device))

    if new_entities:
        _LOGGER.info("Adding %d %s", len(new_entities), "water heaters")
        async_add_devices(new_entities)



class ToshibaDHW(ToshibaAcStateEntity, WaterHeaterEntity):
    """Provides a Toshiba DHW control."""

    # This is the main entity for the device
    _attr_has_entity_name = True
    _attr_name = None

    _attr_supported_features = (
          WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.ON_OFF
    )

    _attr_target_temperature_step = 1
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, toshiba_device: ToshibaAcDevice):
        """Initialize the climate."""
        super().__init__(toshiba_device)

        self._enable_turn_on_off_backwards_compatibility = False
        self._attr_unique_id = f"{self._device.ac_unique_id}_dhw"
        self._attr_name = f"{self._device.name} Hot water"
        self._attr_min_temperature = 20
        self._attr_max_temperature = 65
        self._booster_requested_override: bool | None = None
        self._booster_override_ts = 0.0

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

    def _is_booster_requested(self) -> bool:
        # Booster request is encoded with value 0x10 in HDU payload byte 10.
        # Depending on backend response path we may also observe it on byte 19,
        # so accept either as a request signal.
        b10 = self._raw_byte(10)
        b19 = self._raw_byte(19)
        raw_flag = bool(
            (b10 is not None and (b10 & 0x10))
            or (b19 is not None and (b19 & 0x10))
        )
        coil_active = bool(self._device.electric_coil_dhw_is_active)
        observed = raw_flag or coil_active
        # Keep UI in sync immediately after command; telemetry may lag.
        if self._booster_requested_override is not None and monotonic() - self._booster_override_ts < 180:
            return self._booster_requested_override
        return observed

    def _is_dhw_enabled(self) -> bool:
        b1 = self._raw_byte(1)
        # Captured app protocol: byte1=0x0C means DHW ON, byte1=0x08 means DHW OFF.
        return b1 == 0x0C

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        set_temperature = kwargs[ATTR_TEMPERATURE]
        await set_dhw_temperature(self._device, int(set_temperature))

    @property
    def is_on(self) -> bool | None:
        return self._is_dhw_enabled()


    async def async_turn_on(self) -> None:
        self._booster_requested_override = False
        self._booster_override_ts = monotonic()
        await set_dhw_operation_mode(self._device, STATE_HEAT_PUMP)

    async def async_turn_off(self) -> None:
        self._booster_requested_override = False
        self._booster_override_ts = monotonic()
        await set_dhw_operation_mode(self._device, STATE_OFF)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        # Estia does not expose DHW tank temp directly here; avoid HomeKit fallback to 50C.
        return self.target_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._device.dhw_target_temperature

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 20

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 65

    @property
    def current_operation(self) -> str:
        """Return selected operation mode (booster request state)."""
        if not self._is_dhw_enabled():
            return STATE_OFF
        if self._is_booster_requested():
            return STATE_ELECTRIC
        return STATE_HEAT_PUMP

    @property
    def operation_list(self) -> list[str]:
        """Return operation modes supported by DHW entity."""
        return [STATE_OFF, STATE_HEAT_PUMP, STATE_ELECTRIC]

    @property
    def operation_mode(self) -> str:
        """Return selected operation mode."""
        return self.current_operation

    @property
    def operation_modes(self) -> list[str]:
        """Return operation modes supported by DHW entity."""
        return self.operation_list

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Switch booster state via operation mode."""
        if operation_mode == STATE_OFF:
            self._booster_requested_override = False
        elif operation_mode == STATE_ELECTRIC:
            self._booster_requested_override = True
        elif operation_mode == STATE_HEAT_PUMP:
            self._booster_requested_override = False
        else:
            raise ValueError(f"Unsupported DHW operation mode: {operation_mode}")
        self._booster_override_ts = monotonic()
        await set_dhw_operation_mode(self._device, operation_mode)
