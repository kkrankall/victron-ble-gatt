"""Charge current limit (VE_REG_BAT_MAX_CURRENT), only when control is allowed."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VictronConfigEntry
from . import registers as r
from .client import VictronSmartError
from .coordinator import VictronCoordinator
from .entity import VictronEntity

MIN_LIMIT = 1.0  # stop charging with the Charging switch, not a 0 A limit


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VictronConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.allow_control and r.CHARGE_CURRENT_LIMIT.vreg in coordinator.data.raw:
        async_add_entities([VictronCurrentLimit(coordinator)])


class VictronCurrentLimit(VictronEntity, NumberEntity):
    """Writes the charger's saved setting: it persists through restarts and outages."""

    _attr_translation_key = "charge_current_limit"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_LIMIT
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: VictronCoordinator) -> None:
        super().__init__(coordinator, "charge_current_limit_control")

    @property
    def native_max_value(self) -> float:
        rated = self.coordinator.data.values.get(r.MAX_CHARGER_CURRENT.vreg)
        return float(rated) if rated else 100.0

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.values.get(r.CHARGE_CURRENT_LIMIT.vreg)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_write(r.CHARGE_CURRENT_LIMIT, round(value, 1))
        except VictronSmartError as err:
            raise HomeAssistantError(str(err)) from err
