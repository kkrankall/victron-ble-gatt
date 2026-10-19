"""Diagnostics: the raw registers read from the charger."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import VictronConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VictronConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "instance": data.instance if data else None,
        "product_id": f"0x{data.product_id:04X}" if data and data.product_id else None,
        "firmware": data.firmware if data else None,
        "model": data.model if data else None,
        "raw": {f"0x{k:04X}": v.hex() for k, v in sorted(data.raw.items())} if data else {},
        "total": data.total if data else None,
        "days": data.days if data else {},
    }
