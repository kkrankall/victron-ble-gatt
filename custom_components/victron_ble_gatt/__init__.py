"""Victron MPPT solar chargers over Bluetooth GATT (all VictronConnect readings)."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .client import VictronSmartError
from .const import DOMAIN
from .coordinator import VictronCoordinator

PLATFORMS = [Platform.SENSOR]

type VictronConfigEntry = ConfigEntry[VictronCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _vreg(value: Any) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


PROBE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): cv.string,
        vol.Optional("vregs", default=[]): vol.All(cv.ensure_list, [_vreg]),
        vol.Optional("requests", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("instance"): vol.Coerce(int),
        vol.Optional("get_devices", default=True): cv.boolean,
        vol.Optional("keep_alive", default=True): cv.boolean,
        vol.Optional("subscribe", default=True): cv.boolean,
        vol.Optional("pair", default=False): cv.boolean,
        vol.Optional("listen", default=2.0): vol.All(vol.Coerce(float), vol.Range(0, 30)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the probe action used to explore a device."""

    async def probe(call: ServiceCall) -> ServiceResponse:
        address = call.data[CONF_ADDRESS].upper()
        coordinator: VictronCoordinator | None = next(
            (c for c in hass.data.get(DOMAIN, {}).values() if c.address == address), None
        )
        if coordinator is None:
            raise HomeAssistantError(f"{address} is not set up in {DOMAIN}")
        trace: list[str] = []
        out: dict[str, Any] = {"trace": trace}
        async with coordinator.lock:
            try:
                session = await coordinator.open_session(pair=call.data["pair"], trace=trace.append)
            except VictronSmartError as err:
                out["error"] = str(err)
                return out
            async with session:
                out["services"] = session.describe_services()
                out["control"] = vars(session.info) if session.info else None
                instance = call.data.get("instance")
                if call.data["get_devices"]:
                    devices = await session.get_devices()
                    out["devices"] = devices
                    if instance is None and devices:
                        instance = await session.find_charger(devices, [0xEDDA, 0xED8D])
                instance = instance or 0
                out["instance"] = instance
                if call.data["keep_alive"]:
                    await session.keep_alive()
                if call.data["subscribe"]:
                    # GetValues is ignored until the instance is subscribed
                    await session.subscribe(instance)
                for hexstr in call.data["requests"]:
                    await session.send(bytes.fromhex(hexstr))
                if call.data["vregs"]:
                    values, errors = await session.get_values(instance, call.data["vregs"])
                    out["values"] = {f"0x{k:04X}": v.hex() for k, v in values.items()}
                    out["errors"] = {f"0x{k:04X}": e for k, e in errors.items()}
                await asyncio.sleep(call.data["listen"])
                out["control_events"] = [c.hex() for c in session.control_events]
                out["messages"] = [
                    [x.hex() if isinstance(x, bytes) else x for x in m] for m in session.messages
                ]
        return out

    hass.services.async_register(
        DOMAIN, "probe", probe, schema=PROBE_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VictronConfigEntry) -> bool:
    coordinator = VictronCoordinator(hass, entry, entry.data[CONF_ADDRESS])
    entry.runtime_data = coordinator
    # kept outside runtime_data so the probe action works while setup retries
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: VictronConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VictronConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
