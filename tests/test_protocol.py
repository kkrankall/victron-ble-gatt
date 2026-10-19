"""Protocol and register decoding tests (no hardware)."""

import struct

from custom_components.victron_ble_gatt import protocol as p
from custom_components.victron_ble_gatt import registers as r


def test_char_uuids():
    svc = p.SERVICE_UUIDS[0]
    assert p.char_uuid(svc, p.CONTROL) == "306b0002-b081-4037-83dc-e59fcc3cdfd0"
    assert p.char_uuid(svc, p.LAST_DATA) == "306b0003-b081-4037-83dc-e59fcc3cdfd0"
    assert p.char_uuid(svc, p.DATA) == "306b0004-b081-4037-83dc-e59fcc3cdfd0"


def test_cbor_roundtrip():
    values = [0, 23, 24, 255, 256, 65535, 65536, -1, -500, b"\x01\x02", "abc", [1, [2, 3]], {1: b""}]
    for v in values:
        assert p.cbor_decode_seq(p.cbor_encode(v)) == [v]


def test_requests():
    assert p.get_devices() == bytes([0x01])
    assert p.subscribe(0) == bytes([0x03, 0x00])
    # 05 00 [0xEDBB, 0x0201]
    assert p.get_values(0, [0xEDBB, 0x0201]) == bytes.fromhex("05008219edbb190201")
    # 06 00 [0x93, h'1027']
    assert p.keep_alive() == bytes.fromhex("060082189342" "1027")


def test_indefinite_device_list():
    # captured from Array1
    items = p.cbor_decode_seq(bytes.fromhex("029f000001000301ff"))
    assert p.split_messages(items) == [[2, [0, 0, 1, 0, 3, 1]]]


def test_firmware_release_build():
    assert r.firmware_version(bytes.fromhex("00ff1803")) == "3.18"  # Array1
    assert r.firmware_version(bytes.fromhex("00680100")) == "1.68"  # Array2
    assert r.firmware_version(bytes.fromhex("00031803")) == "3.18-beta-03"


def test_split_message():
    payload = bytes(range(45))
    parts = p.split_message(payload, 20)
    assert [o for o, _ in parts] == [p.DATA, p.DATA, p.LAST_DATA]
    assert b"".join(x for _, x in parts) == payload
    assert p.split_message(b"\x01", 20) == [(p.LAST_DATA, b"\x01")]


def test_split_messages():
    seq = p.cbor_decode_seq(
        p.cbor_encode_seq(8, 0, 0xEDBB, b"\x10\x27", 9, 0, 0x1234, 1, 2, [0, 0xA060])
    )
    assert p.split_messages(seq) == [[8, 0, 0xEDBB, b"\x10\x27"], [9, 0, 0x1234, 1], [2, [0, 0xA060]]]


def test_control_info():
    info = p.ControlInfo.parse(bytes.fromhex("01010004f0f400"))
    assert (info.flags, info.max_instances, info.max_free_chunks, info.chunk_size) == (1, 1, 4, 240)
    assert info.max_att_length == 244
    assert p.ControlInfo.parse(bytes.fromhex("0101000410")).max_att_length == 20


def test_decode_registers():
    assert r.decode(r.PV_VOLTAGE, struct.pack("<H", 4567)) == 45.67
    assert r.decode(r.PV_VOLTAGE, b"\xff\xff") is None
    assert r.decode(r.BATTERY_CURRENT, struct.pack("<h", -15)) == -1.5
    assert r.decode(r.PV_POWER, struct.pack("<I", 123456)) == 1234.56
    assert r.decode(r.MAX_POWER_TODAY, struct.pack("<H", 300)) == 300  # old un16 firmware
    assert r.decode(r.SERIAL_NUMBER, b"HQ2207ABCDE\0\0") == "HQ2207ABCDE"
    assert r.product_id(bytes.fromhex("0060a0ff")) == 0xA060
    assert r.off_reasons(0) == "none"
    assert r.off_reasons(0x01) == "no_input_power"


def test_history():
    total = struct.pack("<BB4BIIHHBH", 1, 0, 0, 0, 0, 0, 280256, 280256, 14512, 2961, 30, 2410)
    t = r.decode_history_total(total)
    assert t["user_yield"] == 2802.56 and t["max_pv_voltage"] == 145.12
    assert t["days_available"] == 30 and t["min_battery_voltage"] == 24.1
    day = struct.pack(
        "<BIIHHB4BHHHIHHH", 0, 630, 0xFFFFFFFF, 2890, 2512, 0, 0, 0, 0, 0, 120, 95, 300, 845, 212, 13044, 700
    )
    d = r.decode_history_day(day)
    assert d["yield"] == 6.3 and d["consumed"] is None and d["max_power"] == 845
    assert d["time_in_absorption"] == 95 and d["max_battery_current"] == 21.2
    assert r.decode_history_day(b"\xff" + bytes(33)) is None
