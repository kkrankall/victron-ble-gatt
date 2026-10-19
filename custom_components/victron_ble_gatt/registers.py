"""Registers ("vregs") of Victron MPPT solar chargers and how to decode them.

IDs, types and scales come from VictronConnect's register catalog; the
path-to-register mapping from its solar charger screens. Values arrive as the
raw little-endian bytes of the register, like the VE.Direct HEX protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any

# fmt, scale; unsigned "all ones" and signed "max positive" mean not available
_FORMATS = {
    "un8": ("<B", 0xFF),
    "un16": ("<H", 0xFFFF),
    "sn16": ("<h", 0x7FFF),
    "un32": ("<I", 0xFFFFFFFF),
    "sn32": ("<i", 0x7FFFFFFF),
}


@dataclass(frozen=True)
class Register:
    vreg: int
    kind: str  # un8/un16/sn16/un32/sn32/string/raw
    scale: float = 1


def decode(register: Register, raw: bytes) -> Any:
    """Decode a register value; None when the device reports 'not available'."""
    if register.kind == "string":
        return raw.split(b"\0", 1)[0].decode(errors="replace").strip() or None
    if register.kind == "raw":
        return bytes(raw)
    fmt, na = _FORMATS[register.kind]
    size = struct.calcsize(fmt)
    if len(raw) < size:
        # MPPTs with old firmware report some un32 registers as un16
        if register.kind in ("un32", "sn32") and len(raw) == 2:
            fmt, na, size = ("<H", 0xFFFF, 2) if register.kind == "un32" else ("<h", 0x7FFF, 2)
        else:
            return None
    value = struct.unpack_from(fmt, raw)[0]
    if value == na:
        return None
    return value / register.scale if register.scale != 1 else value


def encode(register: Register, value: float | int) -> bytes:
    """Raw little-endian bytes for writing ``value`` to a numeric register."""
    fmt, _na = _FORMATS[register.kind]
    return struct.pack(fmt, round(value * register.scale))


# Device identification
PRODUCT_ID = Register(0x0100, "raw")
APP_VERSION = Register(0x0102, "raw")
SERIAL_NUMBER = Register(0x010A, "string")
MODEL_NAME = Register(0x010B, "string")
CUSTOM_NAME = Register(0x010C, "string")
CAPABILITIES = Register(0x0140, "un32")

# Live values
DEVICE_MODE = Register(0x0200, "un8")
DEVICE_STATE = Register(0x0201, "un8")
DEVICE_OFF_REASON = Register(0x0207, "un32")
ERROR_CODE = Register(0xEDDA, "un8")
MPP_MODE = Register(0xEDB3, "un8")
PV_VOLTAGE = Register(0xEDBB, "un16", 100)
PV_CURRENT = Register(0xEDBD, "un16", 10)
PV_POWER = Register(0xEDBC, "un32", 100)
BATTERY_VOLTAGE = Register(0xED8D, "sn16", 100)
BATTERY_CURRENT = Register(0xED8F, "sn16", 10)
BATTERY_TEMPERATURE = Register(0xEDEC, "un16", 100)  # 0.01 K
CHARGER_TEMPERATURE = Register(0xEDDB, "sn16", 100)
LOAD_STATE = Register(0xEDA8, "un8")
LOAD_CURRENT = Register(0xEDAD, "un16", 10)
RELAY_STATE = Register(0x034E, "un8")
YIELD_TODAY = Register(0xEDD3, "un32", 100)
MAX_POWER_TODAY = Register(0xEDD2, "un32")
YIELD_YESTERDAY = Register(0xEDD1, "un32", 100)
MAX_POWER_YESTERDAY = Register(0xEDD0, "un32")
USER_YIELD = Register(0xEDDC, "un32", 100)
SYSTEM_YIELD = Register(0xEDDD, "un32", 100)
PV_ISOLATION = Register(0xEDB1, "un32")
UPTIME = Register(0x0120, "un32")
TRACKERS = Register(0x0244, "un8")

# Settings shown read-only
BATTERY_VOLTAGE_SETTING = Register(0xEDEF, "un8")
CHARGE_CURRENT_LIMIT = Register(0xEDF0, "un16", 10)
ABSORPTION_VOLTAGE = Register(0xEDF7, "un16", 100)
FLOAT_VOLTAGE = Register(0xEDF6, "un16", 100)
MAX_CHARGER_CURRENT = Register(0xEDDF, "un16", 10)

# Multi-tracker models (RS, 250/xx-Tr with several inputs)
TRACKER_VOLTAGE = [Register(0xECCB + 0x10 * i, "un16", 100) for i in range(4)]
TRACKER_CURRENT = [Register(0xECCD + 0x10 * i, "un16", 10) for i in range(4)]
TRACKER_POWER = [Register(0xECCC + 0x10 * i, "un32", 100) for i in range(4)]

HISTORY_TOTAL = 0x104F
HISTORY_DAY0 = 0x1050  # today; 0x1051 yesterday ... 0x106E thirty days ago
HISTORY_DAYS = 31

LIVE = [
    DEVICE_MODE, DEVICE_STATE, DEVICE_OFF_REASON, ERROR_CODE, MPP_MODE, PV_VOLTAGE,
    PV_CURRENT, PV_POWER, BATTERY_VOLTAGE, BATTERY_CURRENT, BATTERY_TEMPERATURE,
    CHARGER_TEMPERATURE, LOAD_STATE, LOAD_CURRENT, RELAY_STATE, YIELD_TODAY,
    MAX_POWER_TODAY, YIELD_YESTERDAY, MAX_POWER_YESTERDAY, USER_YIELD,
    SYSTEM_YIELD, PV_ISOLATION, UPTIME,
]
SETTINGS = [
    BATTERY_VOLTAGE_SETTING, CHARGE_CURRENT_LIMIT, ABSORPTION_VOLTAGE,
    FLOAT_VOLTAGE, MAX_CHARGER_CURRENT,
]
IDENTITY = [PRODUCT_ID, APP_VERSION, SERIAL_NUMBER, MODEL_NAME, CUSTOM_NAME, CAPABILITIES, TRACKERS]
TRACKERS_LIVE = TRACKER_VOLTAGE + TRACKER_CURRENT + TRACKER_POWER

BY_VREG: dict[int, Register] = {
    r.vreg: r for r in IDENTITY + LIVE + SETTINGS + TRACKERS_LIVE
}


def product_id(raw: bytes) -> int | None:
    """VE_REG_PRODUCT_ID: un8 identifier, un16 product id, un8 flags."""
    if len(raw) >= 3:
        return struct.unpack_from("<H", raw, 1)[0]
    if len(raw) == 2:  # VE.Direct MPPTs report a big-endian un16
        return struct.unpack(">H", raw)[0]
    return None


def firmware_version(raw: bytes) -> str | None:
    """VE_REG_APP_VER: un8 identifier, un24 version.

    Newer firmware: 0x0318FF = v3.18 (low byte 0xFF = release, else beta
    build). Older firmware leaves the top byte 0: 0x000168 = v1.68.
    """
    if len(raw) >= 4:
        ver = int.from_bytes(raw[1:4], "little")
    elif len(raw) >= 2:
        ver = int.from_bytes(raw[:2], "little")
    else:
        return None
    if ver in (0xFFFFFF, 0):
        return None
    if ver >> 16 == 0:
        return f"{(ver >> 8) & 0xFF:x}.{ver & 0xFF:02x}"
    text = f"{ver >> 16:x}.{(ver >> 8) & 0xFF:02x}"
    build = ver & 0xFF
    return text if build == 0xFF else f"{text}-beta-{build:02x}"


def _na(value: int, size: int) -> int | None:
    return None if value == (1 << (8 * size)) - 1 else value


def _scaled(value: int | None, scale: float) -> float | None:
    return None if value is None else round(value / scale, 3)


def decode_history_total(raw: bytes) -> dict[str, Any] | None:
    """VE_REG_HISTORY_TOTAL (0x104F), versions 0 and 1."""
    if len(raw) < 19 or raw[0] == 0xFF:
        return None
    _ver, _db, e1, e2, e3, e4, user, system, vpv, vbat, days = struct.unpack_from(
        "<BB4BIIHHB", raw
    )
    out = {
        "last_errors": [e for e in (e1, e2, e3, e4) if e not in (0, 0xFF)],
        "user_yield": _scaled(_na(user, 4), 100),
        "system_yield": _scaled(_na(system, 4), 100),
        "max_pv_voltage": _scaled(_na(vpv, 2), 100),
        "max_battery_voltage": _scaled(_na(vbat, 2), 100),
        "days_available": _na(days, 1),
        "min_battery_voltage": None,
    }
    if raw[0] >= 1 and len(raw) >= 21:
        out["min_battery_voltage"] = _scaled(_na(struct.unpack_from("<H", raw, 19)[0], 2), 100)
    return out


def decode_history_day(raw: bytes) -> dict[str, Any] | None:
    """VE_REG_HISTORY_DAYxx (0x1050..0x106E), version 0."""
    if len(raw) < 34 or raw[0] == 0xFF:
        return None
    (
        _ver, yld, consumed, vbat_max, vbat_min, _db, e1, e2, e3, e4,
        bulk, absorption, float_, pmax, ibat_max, vpv_max, seq,
    ) = struct.unpack_from("<BIIHHB4BHHHIHHH", raw)
    return {
        "yield": _scaled(_na(yld, 4), 100),
        "consumed": _scaled(_na(consumed, 4), 100),
        "max_battery_voltage": _scaled(_na(vbat_max, 2), 100),
        "min_battery_voltage": _scaled(_na(vbat_min, 2), 100),
        "errors": [e for e in (e1, e2, e3, e4) if e not in (0, 0xFF)],
        "time_in_bulk": _na(bulk, 2),
        "time_in_absorption": _na(absorption, 2),
        "time_in_float": _na(float_, 2),
        "max_power": _na(pmax, 4),
        "max_battery_current": _scaled(_na(ibat_max, 2), 10),
        "max_pv_voltage": _scaled(_na(vpv_max, 2), 100),
        "day_sequence": _na(seq, 2),
    }


DEVICE_STATES = {
    0: "off",
    1: "low_power",
    2: "fault",
    3: "bulk",
    4: "absorption",
    5: "float",
    6: "storage",
    7: "equalize",
    245: "starting_up",
    247: "auto_equalize",
    252: "external_control",
}

MPP_MODES = {0: "off", 1: "limited", 2: "tracking"}

DEVICE_MODES = {1: "on", 4: "off"}
MODE_ON = 1
MODE_OFF = 4

# VE.Direct / VE.Smart charger error codes
CHARGER_ERRORS = {
    0: "no_error",
    2: "battery_voltage_too_high",
    3: "remote_temperature_sensor_failure",
    4: "remote_temperature_sensor_failure",
    5: "remote_temperature_sensor_failure",
    6: "remote_battery_voltage_sense_failure",
    7: "remote_battery_voltage_sense_failure",
    8: "remote_battery_voltage_sense_failure",
    11: "battery_high_ripple_voltage",
    14: "battery_temperature_too_low",
    17: "charger_temperature_too_high",
    18: "charger_over_current",
    19: "charger_current_reversed",
    20: "bulk_time_limit_exceeded",
    21: "current_sensor_issue",
    26: "terminals_overheated",
    28: "converter_issue",
    33: "input_voltage_too_high",
    34: "input_current_too_high",
    38: "input_shutdown_battery_voltage",
    39: "input_shutdown_current_flow",
    40: "input_shutdown",
    65: "lost_communication",
    66: "synchronised_charging_config_issue",
    67: "bms_connection_lost",
    68: "network_misconfigured",
    116: "factory_calibration_data_lost",
    117: "invalid_firmware",
    119: "user_settings_invalid",
}

# Readable names for free-text states (the error history); same codes as above
CHARGER_ERROR_TEXT = {
    2: "Battery voltage too high",
    3: "Remote temperature sensor failure",
    4: "Remote temperature sensor failure",
    5: "Remote temperature sensor failure",
    6: "Remote battery voltage sense failure",
    7: "Remote battery voltage sense failure",
    8: "Remote battery voltage sense failure",
    11: "Battery high ripple voltage",
    14: "Battery temperature too low",
    17: "Charger temperature too high",
    18: "Charger over current",
    19: "Charger current reversed",
    20: "Bulk time limit exceeded",
    21: "Current sensor issue",
    26: "Terminals overheated",
    28: "Converter issue",
    33: "Input voltage too high (solar panel)",
    34: "Input current too high (solar panel)",
    38: "Input shutdown (battery voltage too high)",
    39: "Input shutdown (current flow during off mode)",
    40: "Input shutdown",
    65: "Lost communication with a synchronised device",
    66: "Synchronised charging configuration issue",
    67: "BMS connection lost",
    68: "Network misconfigured",
    116: "Factory calibration data lost",
    117: "Invalid or incompatible firmware",
    119: "User settings invalid",
}


def error_text(codes: list[int]) -> str:
    """Readable list of charger error codes, in the order the charger reports them."""
    if not codes:
        return "None"
    return ", ".join(CHARGER_ERROR_TEXT.get(c, f"Error {c}") for c in codes)


OFF_REASONS = {
    0x01: "no_input_power",
    0x02: "hard_power_switch",
    0x04: "soft_power_switch",
    0x08: "remote_input",
    0x10: "internal_reason",
    0x20: "pay_as_you_go",
    0x40: "bms_shutdown",
    0x80: "engine_shutdown",
    0x100: "analysing_input_voltage",
}


def off_reasons(value: int | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "none"
    names = [name for bit, name in OFF_REASONS.items() if value & bit]
    return ",".join(names) if names else f"0x{value:x}"
