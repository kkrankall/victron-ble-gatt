"""Polls a Victron MPPT over GATT: connect, read every register, disconnect."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import registers as r
from .client import Session, VictronSmartError
from .const import (
    CONF_PAIR,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HISTORY_REFRESH_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class VictronData:
    """Everything read from one charger."""

    values: dict[int, Any] = field(default_factory=dict)
    raw: dict[int, bytes] = field(default_factory=dict)
    total: dict[str, Any] | None = None
    days: dict[int, dict[str, Any]] = field(default_factory=dict)
    product_id: int | None = None
    firmware: str | None = None
    serial: str | None = None
    model: str | None = None
    custom_name: str | None = None
    instance: int = 0
    updated: float = 0.0


class VictronCoordinator(DataUpdateCoordinator[VictronData]):
    """One coordinator per charger."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=interval),
        )
        self.address = address
        self.lock = asyncio.Lock()
        self._data = VictronData()
        self._instance: int | None = None
        self._unsupported: set[int] = set()
        self._identity_read = False
        self._history_read = 0.0

    @property
    def pair(self) -> bool:
        return bool(self.config_entry.options.get(CONF_PAIR, True))

    def ble_device(self):
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def open_session(self, **kwargs: Any) -> Session:
        ble_device = self.ble_device()
        if ble_device is None:
            raise VictronSmartError(
                f"{self.address} is not in range of a connectable Bluetooth adapter or proxy"
            )
        return await Session.open(
            ble_device,
            name=self.config_entry.title,
            pair=kwargs.pop("pair", self.pair),
            ble_device_callback=lambda: self.ble_device() or ble_device,
            **kwargs,
        )

    async def _async_update_data(self) -> VictronData:
        async with self.lock:
            try:
                return await self._poll()
            except VictronSmartError as err:
                raise UpdateFailed(str(err)) from err

    async def _poll(self) -> VictronData:
        data = self._data
        session = await self.open_session()
        async with session:
            if self._instance is None:
                devices = await session.get_devices()
                _LOGGER.debug("%s device list %s", self.address, devices)
                self._instance = await session.find_charger(
                    devices, [r.ERROR_CODE.vreg, r.BATTERY_VOLTAGE.vreg]
                )
                if self._instance is None:
                    raise VictronSmartError(f"no solar charger found in device list {devices}")
                _LOGGER.debug("%s charger is instance %s", self.address, self._instance)
            else:
                # GetValues is ignored until the instance is subscribed
                await session.subscribe(self._instance)
            instance = data.instance = self._instance

            vregs = [reg.vreg for reg in r.LIVE + r.SETTINGS]
            if not self._identity_read:
                vregs += [reg.vreg for reg in r.IDENTITY]
            vregs += [r.HISTORY_TOTAL, r.HISTORY_DAY0, r.HISTORY_DAY0 + 1]
            history_due = time.monotonic() - self._history_read > HISTORY_REFRESH_SECONDS
            if history_due:
                vregs += [r.HISTORY_DAY0 + d for d in range(2, r.HISTORY_DAYS)]
            if not self._identity_read or (data.values.get(r.TRACKERS.vreg) or 0) > 1:
                vregs += [reg.vreg for reg in r.TRACKERS_LIVE]
            vregs = [v for v in vregs if v not in self._unsupported]

            values, errors = await session.get_values(instance, vregs)
            # registers pushed after subscribing (uptime, mode, ...) count too
            values = {**session.latest.get(instance, {}), **values}

        if not values:
            raise VictronSmartError(f"no register values received ({len(errors)} errors)")
        for vreg, err in errors.items():
            # missing history days come back as errors until they exist
            if err != "timeout" and vreg in r.BY_VREG:
                self._unsupported.add(vreg)
        _LOGGER.debug(
            "%s read %d registers, %d unsupported, %d timed out",
            self.address,
            len(values),
            sum(1 for e in errors.values() if e != "timeout"),
            sum(1 for e in errors.values() if e == "timeout"),
        )

        for vreg, raw in values.items():
            data.raw[vreg] = raw
            if (reg := r.BY_VREG.get(vreg)) is not None:
                data.values[vreg] = r.decode(reg, raw)
        if r.PRODUCT_ID.vreg in values:
            self._identity_read = True
            data.product_id = r.product_id(values[r.PRODUCT_ID.vreg])
        if r.APP_VERSION.vreg in values:
            data.firmware = r.firmware_version(values[r.APP_VERSION.vreg])
        data.serial = data.values.get(r.SERIAL_NUMBER.vreg) or data.serial
        data.model = data.values.get(r.MODEL_NAME.vreg) or data.model
        data.custom_name = data.values.get(r.CUSTOM_NAME.vreg) or data.custom_name
        if r.HISTORY_TOTAL in values:
            data.total = r.decode_history_total(values[r.HISTORY_TOTAL])
        for day in range(r.HISTORY_DAYS):
            if (raw := values.get(r.HISTORY_DAY0 + day)) is not None:
                if (decoded := r.decode_history_day(raw)) is not None:
                    data.days[day] = decoded
        if history_due and any(r.HISTORY_DAY0 + d in values for d in range(2, r.HISTORY_DAYS)):
            self._history_read = time.monotonic()
        data.updated = time.time()
        return data
