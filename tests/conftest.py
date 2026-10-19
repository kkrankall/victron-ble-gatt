"""A simulated Victron SmartSolar MPPT speaking VE.Smart over a fake Bleak client."""

from __future__ import annotations

import asyncio
import struct
import time
from types import SimpleNamespace
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import pytest

from custom_components.victron_ble_gatt import protocol as p

pytest_plugins = "pytest_homeassistant_custom_component"

ADDRESS = "C0:FF:EE:00:00:01"
SERVICE = p.SERVICE_UUIDS[0]
ADV = bytes.fromhex("100215a1013300000000000000000000000000")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _day(yld: int, pmax: int) -> bytes:
    return struct.pack(
        "<BIIHHB4BHHHIHHH", 0, yld, 0xFFFFFFFF, 2890, 2512, 0, 0, 0, 0, 0, 120, 95, 300, pmax, 212, 13044, 700
    )


class FakeMppt:
    def __init__(self) -> None:
        self.registers: dict[int, bytes] = {
            0x0100: bytes.fromhex("0015a1ff"),
            0x0102: bytes.fromhex("00ff1803"),
            0x010A: b"HQ2222XXXXX\0",
            0x010B: b"SmartSolar MPPT VE.Can 150/70\0",
            0x010C: b"Array1\0",
            0x0140: struct.pack("<I", 0x1E05),
            0x0244: bytes([1]),
            0x0200: bytes([1]),
            0x0201: bytes([5]),
            0x0207: struct.pack("<I", 0),
            0xEDDA: bytes([0]),
            0xEDB3: bytes([2]),
            0xEDBB: struct.pack("<H", 9876),
            0xEDBD: struct.pack("<H", 52),
            0xEDBC: struct.pack("<I", 51355),
            0xED8D: struct.pack("<h", 2712),
            0xED8F: struct.pack("<h", 186),
            0xEDEC: b"\xff\xff",
            0xEDD3: struct.pack("<I", 412),
            0xEDD2: struct.pack("<I", 1210),
            0xEDD1: struct.pack("<I", 630),
            0xEDD0: struct.pack("<I", 845),
            0xEDDC: struct.pack("<I", 280256),
            0xEDDD: struct.pack("<I", 280256),
            0x0120: struct.pack("<I", 86400 * 3),
            0xEDEF: bytes([24]),
            0xEDF0: struct.pack("<H", 700),
            0xEDF7: struct.pack("<H", 2840),
            0xEDF6: struct.pack("<H", 2760),
            0xEDDF: struct.pack("<H", 700),
            0x104F: struct.pack("<BB4BIIHHBH", 1, 0, 0, 0, 0, 0, 280256, 280256, 14512, 2961, 2, 2410),
            0x1050: _day(412, 1210),
            0x1051: _day(630, 845),
            0x1052: _day(170, 300),
        }
        self.connections = 0
        self.writes: list[tuple[int, bytes]] = []
        self.refuse: set[int] = set()
        self.requests: list[list] = []
        self.grants: list[bytes] = []


class FakeClient:
    def __init__(self, mppt: FakeMppt) -> None:
        self.mppt = mppt
        mppt.connections += 1
        self.address = ADDRESS
        self.is_connected = True
        self._callbacks: dict[str, object] = {}
        self._rx = b""
        self.subscribed: set[int] = set()
        uuids = {off: p.char_uuid(SERVICE, off) for off in (p.CONTROL, p.LAST_DATA, p.DATA)}
        self._names = {v: k for k, v in uuids.items()}
        chars = {
            u: SimpleNamespace(uuid=u, handle=0x20 + off, properties=["read", "write", "write-without-response", "notify"])
            for off, u in uuids.items()
        }
        self._chars = chars
        service = SimpleNamespace(
            uuid=SERVICE,
            characteristics=list(chars.values()),
            get_characteristic=chars.get,
        )
        self.services = [service]

    async def start_notify(self, char, callback) -> None:
        self._callbacks[self._names[char.uuid]] = callback

    async def read_gatt_char(self, char) -> bytearray:
        assert self._names[char.uuid] == p.CONTROL
        return bytearray(bytes.fromhex("010100" "04" "f0" "f400"))

    async def write_gatt_char(self, char, data, response) -> None:
        which = self._names[char.uuid]
        data = bytes(data)
        if which == p.CONTROL:
            self.mppt.grants.append(data)
            return
        self._rx += data
        if which == p.DATA:
            return
        chunk, self._rx = self._rx, b""
        replies = []
        for msg in p.split_messages(p.cbor_decode_seq(chunk)):
            self.mppt.requests.append(msg)
            replies.extend(self._handle(msg))
        loop = asyncio.get_running_loop()
        for reply in replies:
            loop.call_soon(self._callbacks[p.LAST_DATA], char, bytearray(reply))
        loop.call_soon(self._callbacks[p.CONTROL], char, bytearray(b"\xf9\x01"))

    def _handle(self, msg: list) -> list[bytes]:
        op = msg[0]
        if op == p.OP_GET_DEVICES:
            # as captured from Array1: indefinite array, BLE module 0, UART 1, charger 3
            return [bytes.fromhex("029f000001000301ff")]
        if op == p.OP_SET_VALUE:
            vreg, raw = msg[2]
            if vreg in self.mppt.refuse:
                return [p.cbor_encode_seq(p.OP_VALUE_RESPONSE, msg[1], vreg, 0x8300)]
            self.mppt.registers[vreg] = raw
            self.mppt.writes.append((vreg, raw))
            # the real charger echoes the register's new contents
            return [p.cbor_encode_seq(p.OP_VALUE, msg[1], vreg, raw)]
        if op == p.OP_SUBSCRIBE:
            self.subscribed.add(msg[1])
            return [p.cbor_encode_seq(p.OP_RESPONSE, 0, p.OP_SUBSCRIBE, 0)]
        if op == p.OP_GET_VALUES:
            if msg[1] not in self.subscribed:
                return []  # the real device stays silent until subscribed
            out = b""
            for vreg in msg[2]:
                if msg[1] == 0 and vreg == 0x0100:
                    out += p.cbor_encode_seq(p.OP_VALUE, 0, vreg, bytes.fromhex("009ba1fe"))
                elif msg[1] == 3 and vreg in self.mppt.registers:
                    out += p.cbor_encode_seq(p.OP_VALUE, msg[1], vreg, self.mppt.registers[vreg])
                else:
                    out += p.cbor_encode_seq(p.OP_VALUE_RESPONSE, msg[1], vreg, 1)
            return [out]
        return []

    async def disconnect(self) -> None:
        self.is_connected = False

    async def pair(self) -> bool:
        return True


@pytest.fixture
def mppt():
    fake = FakeMppt()

    async def connect(_cls, _device, _name, **_kwargs):
        return FakeClient(fake)

    with patch("custom_components.victron_ble_gatt.client.establish_connection", connect):
        yield fake


def service_info(name: str = "Array1", rssi: int = -70, manufacturer_id: int | None = p.VICTRON_MANUFACTURER_ID):
    """Advertisement; setup tests use a foreign manufacturer id so core victron_ble stays out."""
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    device = BLEDevice(ADDRESS, name, {})
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={manufacturer_id: ADV} if manufacturer_id is not None else {},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=ADDRESS,
        rssi=rssi,
        manufacturer_data=adv.manufacturer_data,
        service_data={},
        service_uuids=[],
        source="local",
        device=device,
        advertisement=adv,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


def inject(hass, info) -> None:
    from homeassistant.components.bluetooth.api import _get_manager

    _get_manager(hass).scanner_adv_received(info)
