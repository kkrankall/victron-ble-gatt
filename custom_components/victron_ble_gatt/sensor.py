"""Sensors for every reading VictronConnect shows for an MPPT."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VictronConfigEntry
from . import registers as r
from .const import DOMAIN, MANUFACTURER
from .coordinator import VictronCoordinator, VictronData

V = UnitOfElectricPotential.VOLT
A = UnitOfElectricCurrent.AMPERE
W = UnitOfPower.WATT
KWH = UnitOfEnergy.KILO_WATT_HOUR
MIN = UnitOfTime.MINUTES


@dataclass(frozen=True, kw_only=True)
class VictronSensorDescription(SensorEntityDescription):
    value_fn: Callable[[VictronData], Any]
    exists_fn: Callable[[VictronData], bool]
    attrs_fn: Callable[[VictronData], dict[str, Any]] | None = None


def _reg(reg: r.Register, convert: Callable[[Any], Any] | None = None):
    def value(data: VictronData) -> Any:
        val = data.values.get(reg.vreg)
        return convert(val) if convert is not None and val is not None else val

    def exists(data: VictronData) -> bool:
        return reg.vreg in data.raw

    return value, exists


def _enum(table: dict[int, str]) -> Callable[[Any], str | None]:
    return lambda v: table.get(int(v))


def _kelvin(value: float) -> float:
    return round(value - 273.15, 2)


def reg_sensor(key: str, reg: r.Register, convert=None, **kwargs: Any) -> VictronSensorDescription:
    value_fn, exists_fn = _reg(reg, convert)
    return VictronSensorDescription(
        key=key, translation_key=key, value_fn=value_fn, exists_fn=exists_fn, **kwargs
    )


def day_sensor(key: str, day: int, field: str, **kwargs: Any) -> VictronSensorDescription:
    return VictronSensorDescription(
        key=key,
        translation_key=key,
        value_fn=lambda d: (d.days.get(day) or {}).get(field),
        exists_fn=lambda d: day in d.days,
        **kwargs,
    )


def total_sensor(key: str, field: str, **kwargs: Any) -> VictronSensorDescription:
    return VictronSensorDescription(
        key=key,
        translation_key=key,
        value_fn=lambda d: (d.total or {}).get(field),
        exists_fn=lambda d: d.total is not None,
        **kwargs,
    )


def _volt(**kw: Any) -> dict[str, Any]:
    return {
        "native_unit_of_measurement": V,
        "device_class": SensorDeviceClass.VOLTAGE,
        "suggested_display_precision": 2,
        **kw,
    }


def _amp(**kw: Any) -> dict[str, Any]:
    return {
        "native_unit_of_measurement": A,
        "device_class": SensorDeviceClass.CURRENT,
        "suggested_display_precision": 1,
        **kw,
    }


def _power(**kw: Any) -> dict[str, Any]:
    return {"native_unit_of_measurement": W, "device_class": SensorDeviceClass.POWER, **kw}


def _energy(**kw: Any) -> dict[str, Any]:
    return {
        "native_unit_of_measurement": KWH,
        "device_class": SensorDeviceClass.ENERGY,
        "suggested_display_precision": 2,
        **kw,
    }


def _minutes(**kw: Any) -> dict[str, Any]:
    return {"native_unit_of_measurement": MIN, "device_class": SensorDeviceClass.DURATION, **kw}


MEAS = {"state_class": SensorStateClass.MEASUREMENT}
DIAG = {"entity_category": EntityCategory.DIAGNOSTIC}

SENSORS: list[VictronSensorDescription] = [
    # live
    reg_sensor("pv_power", r.PV_POWER, **_power(**MEAS)),
    reg_sensor("pv_voltage", r.PV_VOLTAGE, **_volt(**MEAS)),
    reg_sensor("pv_current", r.PV_CURRENT, **_amp(**MEAS)),
    reg_sensor("battery_voltage", r.BATTERY_VOLTAGE, **_volt(**MEAS)),
    reg_sensor("battery_current", r.BATTERY_CURRENT, **_amp(**MEAS)),
    reg_sensor(
        "battery_temperature",
        r.BATTERY_TEMPERATURE,
        _kelvin,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        **MEAS,
    ),
    reg_sensor(
        "charger_temperature",
        r.CHARGER_TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        **MEAS,
    ),
    reg_sensor(
        "mode",
        r.DEVICE_MODE,
        _enum(r.DEVICE_MODES),
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(r.DEVICE_MODES.values())),
        **DIAG,
    ),
    reg_sensor(
        "state",
        r.DEVICE_STATE,
        _enum(r.DEVICE_STATES),
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(r.DEVICE_STATES.values())),
    ),
    reg_sensor(
        "error",
        r.ERROR_CODE,
        _enum(r.CHARGER_ERRORS),
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(r.CHARGER_ERRORS.values())),
    ),
    reg_sensor(
        "mpp_mode",
        r.MPP_MODE,
        _enum(r.MPP_MODES),
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(r.MPP_MODES.values())),
    ),
    reg_sensor("off_reason", r.DEVICE_OFF_REASON, r.off_reasons, **DIAG),
    reg_sensor("load_current", r.LOAD_CURRENT, **_amp(**MEAS)),
    reg_sensor("load_state", r.LOAD_STATE, _enum({0: "off", 1: "on"})),
    reg_sensor("relay_state", r.RELAY_STATE, _enum({0: "open", 1: "closed"}), **DIAG),
    reg_sensor(
        "pv_isolation_resistance",
        r.PV_ISOLATION,
        native_unit_of_measurement="Ω",
        **MEAS,
        **DIAG,
    ),
    reg_sensor(
        "uptime",
        r.UPTIME,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        suggested_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=1,
        **DIAG,
    ),
    # yields
    reg_sensor("yield_today", r.YIELD_TODAY, **_energy(state_class=SensorStateClass.TOTAL_INCREASING)),
    reg_sensor("max_power_today", r.MAX_POWER_TODAY, **_power()),
    reg_sensor("yield_yesterday", r.YIELD_YESTERDAY, **_energy()),
    reg_sensor("max_power_yesterday", r.MAX_POWER_YESTERDAY, **_power()),
    reg_sensor("total_yield", r.USER_YIELD, **_energy(state_class=SensorStateClass.TOTAL_INCREASING)),
    reg_sensor(
        "system_yield",
        r.SYSTEM_YIELD,
        **_energy(state_class=SensorStateClass.TOTAL_INCREASING),
        **DIAG,
    ),
    # today's and yesterday's history records
    day_sensor("time_in_bulk_today", 0, "time_in_bulk", **_minutes()),
    day_sensor("time_in_absorption_today", 0, "time_in_absorption", **_minutes()),
    day_sensor("time_in_float_today", 0, "time_in_float", **_minutes()),
    day_sensor("max_battery_voltage_today", 0, "max_battery_voltage", **_volt()),
    day_sensor("min_battery_voltage_today", 0, "min_battery_voltage", **_volt()),
    day_sensor("max_battery_current_today", 0, "max_battery_current", **_amp()),
    day_sensor("max_pv_voltage_today", 0, "max_pv_voltage", **_volt()),
    day_sensor("time_in_bulk_yesterday", 1, "time_in_bulk", **_minutes()),
    day_sensor("time_in_absorption_yesterday", 1, "time_in_absorption", **_minutes()),
    day_sensor("time_in_float_yesterday", 1, "time_in_float", **_minutes()),
    day_sensor("max_battery_voltage_yesterday", 1, "max_battery_voltage", **_volt()),
    day_sensor("min_battery_voltage_yesterday", 1, "min_battery_voltage", **_volt()),
    day_sensor("max_battery_current_yesterday", 1, "max_battery_current", **_amp()),
    day_sensor("max_pv_voltage_yesterday", 1, "max_pv_voltage", **_volt()),
    # lifetime totals
    total_sensor("max_pv_voltage_overall", "max_pv_voltage", **_volt()),
    total_sensor("max_battery_voltage_overall", "max_battery_voltage", **_volt()),
    total_sensor("min_battery_voltage_overall", "min_battery_voltage", **_volt()),
    VictronSensorDescription(
        key="last_errors",
        translation_key="last_errors",
        value_fn=lambda d: r.error_text((d.total or {}).get("last_errors", [])),
        exists_fn=lambda d: d.total is not None,
        attrs_fn=lambda d: {"codes": (d.total or {}).get("last_errors", [])},
        **DIAG,
    ),
    VictronSensorDescription(
        key="history",
        translation_key="history",
        native_unit_of_measurement=UnitOfTime.DAYS,
        value_fn=lambda d: len(d.days),
        exists_fn=lambda d: bool(d.days),
        attrs_fn=lambda d: {"days": [{"day": -k, **v} for k, v in sorted(d.days.items())]},
    ),
    # settings, read-only
    reg_sensor(
        "battery_voltage_setting",
        r.BATTERY_VOLTAGE_SETTING,
        native_unit_of_measurement=V,
        device_class=SensorDeviceClass.VOLTAGE,
        **DIAG,
    ),
    reg_sensor("charge_current_limit", r.CHARGE_CURRENT_LIMIT, **_amp(**DIAG)),
    reg_sensor("absorption_voltage", r.ABSORPTION_VOLTAGE, **_volt(**DIAG)),
    reg_sensor("float_voltage", r.FLOAT_VOLTAGE, **_volt(**DIAG)),
    reg_sensor("max_charger_current", r.MAX_CHARGER_CURRENT, **_amp(**DIAG)),
]

for _i in range(4):
    SENSORS += [
        reg_sensor(f"tracker_{_i + 1}_voltage", r.TRACKER_VOLTAGE[_i], **_volt(**MEAS)),
        reg_sensor(f"tracker_{_i + 1}_current", r.TRACKER_CURRENT[_i], **_amp(**MEAS)),
        reg_sensor(f"tracker_{_i + 1}_power", r.TRACKER_POWER[_i], **_power(**MEAS)),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VictronConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    added: set[str] = set()

    def add_new() -> None:
        new = [
            VictronSensor(coordinator, desc)
            for desc in SENSORS
            if desc.key not in added and desc.exists_fn(coordinator.data)
        ]
        added.update(s.entity_description.key for s in new)
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class VictronSensor(CoordinatorEntity[VictronCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: VictronSensorDescription

    def __init__(self, coordinator: VictronCoordinator, description: VictronSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address = coordinator.address
        self._attr_unique_id = f"{address}_{description.key}"
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

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
