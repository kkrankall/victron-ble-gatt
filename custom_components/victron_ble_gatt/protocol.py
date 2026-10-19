"""Victron "VE.Smart" GATT protocol used by VictronConnect for Smart devices.

Recovered from VictronConnect 6.43 (macOS) by static analysis of
``VeSmartService``/``VeService``:

* Service ``306b0001-b081-4037-83dc-e59fcc3cdfd0`` (some products use the
  ``...dfd1`` variant). Its characteristics are the service UUID with the
  4th byte incremented: +1 control, +2 "last data", +3 "data".
* Notifications are enabled on control, last data and data, then control is
  read: ``flags u8, max_instances u16, max_free_chunks u8, chunk_size u8,
  [max_att_length u16]`` (little endian, ATT length defaults to 20).
* The client then writes ``FA 80 FF`` to control and grants the device
  receive credits with ``F9 <n>`` (``F9 80`` first, then ``F9 n`` after every
  65 chunks it has received). The device grants credits the same way.
* A chunk (at most ``chunk_size`` bytes) holds one or more CBOR messages
  ``opcode, instance, ...`` back to back. It is split into ATT-sized pieces:
  all but the last go to data, the last one to last data, which completes the
  chunk. Replies arrive the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any

SERVICE_UUIDS = (
    "306b0001-b081-4037-83dc-e59fcc3cdfd0",
    "306b0001-b081-4037-83dc-e59fcc3cdfd1",
)
DEVICE_SERVICE_UUID = "97580001-ddf1-48be-b73e-182664615d8e"

VICTRON_MANUFACTURER_ID = 0x02E1


def char_uuid(service_uuid: str, offset: int) -> str:
    """Return the characteristic UUID at ``offset`` from the service UUID."""
    head = int(service_uuid[:8], 16) + offset
    return f"{head:08x}{service_uuid[8:]}"


CONTROL = 1
LAST_DATA = 2
DATA = 3

# Control opcodes.
CTRL_HELLO = bytes([0xFA, 0x80, 0xFF])
CTRL_GRANT = 0xF9  # "ready to receive": n more chunks may be sent
CTRL_ERROR = 0xF8
INITIAL_GRANT = 0x80
REGRANT_AFTER = 0x41

# Request opcodes (client -> device).
OP_GET_DEVICES = 1
OP_SUBSCRIBE = 3
OP_GET_VALUES = 5
OP_SET_VALUE = 6
OP_GET_PATH_LIST = 10
OP_GET_PATH_VALUES = 11
OP_SET_PATH_VALUES = 12

# Reply opcodes (device -> client) and their parameter counts.
OP_DEVICE_LIST = 2
OP_RESPONSE = 7  # instance, request opcode, result
OP_VALUE = 8  # instance, vreg, raw little-endian bytes
OP_VALUE_RESPONSE = 9  # instance, vreg, error (set/get refused)
OP_PATH_LIST = 13
OP_NEW_PATH = 14
OP_PATH_VALUE = 15
OP_PATH_VALUE_RESPONSE = 16
_ARITY = {OP_DEVICE_LIST: 1, OP_PATH_LIST: 2}

VREG_KEEP_ALIVE = 0x0093
KEEP_ALIVE_MS = 10000


class CborError(ValueError):
    """Raised for malformed CBOR."""


@dataclass(frozen=True)
class CborTag:
    tag: int
    value: Any


@dataclass(frozen=True)
class CborSimple:
    value: int


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([major << 5 | value])
    if value < 0x100:
        return bytes([major << 5 | 24, value])
    if value < 0x10000:
        return bytes([major << 5 | 25]) + struct.pack(">H", value)
    if value < 0x100000000:
        return bytes([major << 5 | 26]) + struct.pack(">I", value)
    return bytes([major << 5 | 27]) + struct.pack(">Q", value)


def cbor_encode(value: Any) -> bytes:
    """Encode one CBOR data item."""
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    if isinstance(value, str):
        raw = value.encode()
        return _head(3, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(
            cbor_encode(k) + cbor_encode(v) for k, v in value.items()
        )
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    raise TypeError(f"cannot CBOR-encode {type(value).__name__}")


def cbor_encode_seq(*values: Any) -> bytes:
    """Encode a CBOR sequence (items back to back, no wrapping array)."""
    return b"".join(cbor_encode(v) for v in values)


def _decode(buf: bytes, pos: int) -> tuple[Any, int]:
    if pos >= len(buf):
        raise CborError("truncated")
    ib = buf[pos]
    pos += 1
    major, info = ib >> 5, ib & 0x1F
    if major == 7:
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        if info in (22, 23):
            return None, pos
        if info == 25:
            half = struct.unpack_from(">e", buf, pos)[0]
            return half, pos + 2
        if info == 26:
            return struct.unpack_from(">f", buf, pos)[0], pos + 4
        if info == 27:
            return struct.unpack_from(">d", buf, pos)[0], pos + 8
        if info < 24:
            return CborSimple(info), pos
        if info == 24:
            return CborSimple(buf[pos]), pos + 1
        raise CborError(f"unsupported simple {info}")
    if info == 31:
        return _decode_indefinite(buf, pos, major)
    if info < 24:
        arg = info
    elif info == 24:
        arg = buf[pos]
        pos += 1
    elif info == 25:
        arg = struct.unpack_from(">H", buf, pos)[0]
        pos += 2
    elif info == 26:
        arg = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
    elif info == 27:
        arg = struct.unpack_from(">Q", buf, pos)[0]
        pos += 8
    else:
        raise CborError(f"reserved additional info {info}")
    if pos > len(buf):
        raise CborError("truncated")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        end = pos + arg
        if end > len(buf):
            raise CborError("truncated string")
        raw = buf[pos:end]
        return (raw if major == 2 else raw.decode(errors="replace")), end
    if major == 4:
        items = []
        for _ in range(arg):
            item, pos = _decode(buf, pos)
            items.append(item)
        return items, pos
    if major == 5:
        out = {}
        for _ in range(arg):
            key, pos = _decode(buf, pos)
            val, pos = _decode(buf, pos)
            out[key if not isinstance(key, list) else tuple(key)] = val
        return out, pos
    item, pos = _decode(buf, pos)
    return CborTag(arg, item), pos


def _decode_indefinite(buf: bytes, pos: int, major: int) -> tuple[Any, int]:
    """Indefinite-length array/map/string; the device list uses these."""
    items: list[Any] = []
    while True:
        if pos >= len(buf):
            raise CborError("truncated indefinite item")
        if buf[pos] == 0xFF:
            pos += 1
            break
        item, pos = _decode(buf, pos)
        items.append(item)
    if major == 4:
        return items, pos
    if major == 5:
        if len(items) % 2:
            raise CborError("odd indefinite map")
        return dict(zip(items[0::2], items[1::2], strict=True)), pos
    if major == 2:
        return b"".join(items), pos
    if major == 3:
        return "".join(items), pos
    raise CborError(f"indefinite length not allowed for major type {major}")


def cbor_decode_seq(buf: bytes) -> list[Any]:
    """Decode every item of a CBOR sequence."""
    out = []
    pos = 0
    while pos < len(buf):
        item, pos = _decode(buf, pos)
        out.append(item)
    return out


def split_messages(items: list[Any]) -> list[list[Any]]:
    """Group a decoded chunk into ``[opcode, *params]`` messages."""
    out = []
    i = 0
    while i < len(items):
        op = items[i]
        if not isinstance(op, int):
            raise CborError(f"opcode is not an unsigned: {op!r}")
        n = _ARITY.get(op, 3)
        out.append(items[i : i + 1 + n])
        i += 1 + n
    return out


@dataclass
class ControlInfo:
    flags: int
    max_instances: int
    max_free_chunks: int
    chunk_size: int
    max_att_length: int

    @classmethod
    def parse(cls, raw: bytes) -> ControlInfo:
        if len(raw) < 5:
            raise ValueError(f"control data too short: {raw.hex()}")
        flags, max_instances, max_free, chunk = struct.unpack_from("<BHBB", raw)
        att = struct.unpack_from("<H", raw, 5)[0] if len(raw) >= 7 else 20
        return cls(flags, max_instances, max_free, chunk, max(att, 20))


def split_message(payload: bytes, att_length: int) -> list[tuple[int, bytes]]:
    """Split a message into (characteristic offset, piece) writes."""
    pieces = [payload[i : i + att_length] for i in range(0, len(payload), att_length)]
    return [(DATA, p) for p in pieces[:-1]] + [(LAST_DATA, pieces[-1])]


def get_devices() -> bytes:
    return cbor_encode_seq(OP_GET_DEVICES)


def subscribe(instance: int) -> bytes:
    return cbor_encode_seq(OP_SUBSCRIBE, instance)


def get_values(instance: int, vregs: list[int]) -> bytes:
    return cbor_encode_seq(OP_GET_VALUES, instance, list(vregs))


def set_value(instance: int, vreg: int, value: bytes) -> bytes:
    return cbor_encode_seq(OP_SET_VALUE, instance, [vreg, bytes(value)])


def keep_alive() -> bytes:
    return set_value(0, VREG_KEEP_ALIVE, struct.pack("<H", KEEP_ALIVE_MS))
