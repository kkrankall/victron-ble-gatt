"""Shared base for the charger's entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import VictronCoordinator


class VictronEntity(CoordinatorEntity[VictronCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: VictronCoordinator, key: str) -> None:
        super().__init__(coordinator)
        address = coordinator.address
        self._attr_unique_id = f"{address}_{key}"
        data = coordinator.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=data.model,
            model_id=f"0x{data.product_id:04X}" if data.product_id is not None else None,
            serial_number=data.serial,
            sw_version=data.firmware,
        )
