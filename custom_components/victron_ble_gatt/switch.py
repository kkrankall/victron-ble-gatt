"""Charger on/off (VE_REG_DEVICE_MODE), only when control is allowed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VictronConfigEntry
from . import registers as r
from .client import VictronSmartError
from .coordinator import VictronCoordinator
from .entity import VictronEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VictronConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.allow_control and r.DEVICE_MODE.vreg in coordinator.data.raw:
        async_add_entities([VictronChargingSwitch(coordinator)])


class VictronChargingSwitch(VictronEntity, SwitchEntity):
    """Off stops charging (the charger reports "soft power switch" as off reason)."""

    _attr_translation_key = "charging"

    def __init__(self, coordinator: VictronCoordinator) -> None:
        super().__init__(coordinator, "charging")

    @property
    def is_on(self) -> bool | None:
        mode = self.coordinator.data.values.get(r.DEVICE_MODE.vreg)
        return None if mode is None else mode == r.MODE_ON

    async def _set(self, mode: int) -> None:
        try:
            await self.coordinator.async_write(r.DEVICE_MODE, mode)
        except VictronSmartError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(r.MODE_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(r.MODE_OFF)
