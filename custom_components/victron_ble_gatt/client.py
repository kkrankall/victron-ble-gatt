"""One GATT session with a Victron Smart device (see protocol.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import logging
import time
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 3
REPLY_TIMEOUT = 8.0
CREDIT_TIMEOUT = 3.0


class VictronSmartError(Exception):
    """The device could not be reached or did not answer."""


class Session:
    """A connected VE.Smart session; use ``async with Session.connect(...)``."""

    def __init__(self, client: BleakClient, trace: Callable[[str], None] | None = None) -> None:
        self._client = client
        self._trace = trace
        self._t0 = time.monotonic()
        self.info: p.ControlInfo | None = None
        self.messages: list[list[Any]] = []
        self.control_events: list[bytes] = []
        # newest value per (instance, vreg), including values pushed after subscribe
        self.latest: dict[int, dict[int, bytes]] = {}
        self._chars: dict[int, BleakGATTCharacteristic] = {}
        self._rx = bytearray()
        self._rx_since_grant = 0
        self._credits = 0
        self._credit_event = asyncio.Event()
        self._message_event = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # -- connection -------------------------------------------------------

    @classmethod
    async def open(
        cls,
        ble_device: BLEDevice,
        *,
        name: str,
        pair: bool = False,
        trace: Callable[[str], None] | None = None,
        ble_device_callback: Callable[[], BLEDevice] | None = None,
    ) -> Session:
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                name,
                max_attempts=CONNECT_ATTEMPTS,
                ble_device_callback=ble_device_callback,
            )
        except (BleakError, TimeoutError) as err:
            raise VictronSmartError(f"could not connect to {name}: {err}") from err
        session = cls(client, trace)
        try:
            if pair:
                session._log("pairing")
                try:
                    await client.pair()
                    session._log("paired")
                except (BleakError, NotImplementedError) as err:
                    session._log(f"pair failed: {err!r}")
            await session._start()
        except BaseException:
            await session.close()
            raise
        return session

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        try:
            await self._client.disconnect()
        except BleakError as err:  # pragma: no cover - best effort
            _LOGGER.debug("disconnect failed: %s", err)

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _log(self, text: str) -> None:
        line = f"{time.monotonic() - self._t0:6.3f} {text}"
        _LOGGER.debug("%s %s", self._client.address, line)
        if self._trace is not None:
            self._trace(line)

    def describe_services(self) -> list[dict[str, Any]]:
        return [
            {
                "uuid": s.uuid,
                "characteristics": [
                    {"uuid": c.uuid, "handle": c.handle, "properties": list(c.properties)}
                    for c in s.characteristics
                ],
            }
            for s in self._client.services
        ]

    async def _start(self) -> None:
        services = {s.uuid.lower(): s for s in self._client.services}
        service_uuid = next((u for u in p.SERVICE_UUIDS if u in services), None)
        if service_uuid is None:
            raise VictronSmartError(
                "the device has no VE.Smart service; is it a Victron Smart product?"
            )
        service = services[service_uuid]
        for offset in (p.CONTROL, p.LAST_DATA, p.DATA):
            char = service.get_characteristic(p.char_uuid(service_uuid, offset))
            if char is None:
                raise VictronSmartError(f"characteristic +{offset} missing")
            self._chars[offset] = char
        try:
            await self._client.start_notify(self._chars[p.CONTROL], self._on_control)
            await self._client.start_notify(self._chars[p.LAST_DATA], self._on_last_data)
            await self._client.start_notify(self._chars[p.DATA], self._on_data)
            raw = bytes(await self._client.read_gatt_char(self._chars[p.CONTROL]))
        except BleakError as err:
            raise VictronSmartError(f"VE.Smart service refused access: {err}") from err
        self._log(f"control read {raw.hex()}")
        self.info = p.ControlInfo.parse(raw)
        self._credits = self.info.max_free_chunks
        self._log(f"control {self.info}")
        await self._write(p.CONTROL, p.CTRL_HELLO)
        await self._write(p.CONTROL, bytes([p.CTRL_GRANT, p.INITIAL_GRANT]))

    # -- transport --------------------------------------------------------

    async def _write(self, offset: int, payload: bytes) -> None:
        char = self._chars[offset]
        response = "write-without-response" not in char.properties
        self._log(f">> {('', 'ctrl', 'last', 'data')[offset]} {payload.hex()}")
        try:
            await self._client.write_gatt_char(char, payload, response=response)
        except BleakError as err:
            raise VictronSmartError(f"write failed: {err}") from err

    async def send(self, payload: bytes) -> None:
        """Send one chunk (one or more encoded messages)."""
        assert self.info is not None
        if len(payload) > max(self.info.chunk_size, 1):
            raise ValueError(f"chunk of {len(payload)} bytes exceeds {self.info.chunk_size}")
        if self._credits <= 0:
            self._credit_event.clear()
            try:
                async with asyncio.timeout(CREDIT_TIMEOUT):
                    await self._credit_event.wait()
            except TimeoutError:
                self._log("no send credit from device; sending anyway")
        self._credits -= 1
        for offset, piece in p.split_message(payload, self.info.max_att_length):
            await self._write(offset, piece)

    def _on_control(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        raw = bytes(data)
        self._log(f"<< ctrl {raw.hex()}")
        self.control_events.append(raw)
        if raw[:1] == bytes([p.CTRL_GRANT]) and len(raw) >= 2:
            self._credits += raw[1]
            if self.info is not None:
                self._credits = min(self._credits, max(self.info.max_free_chunks, raw[1]))
            self._credit_event.set()
        elif raw[:1] == bytes([p.CTRL_ERROR]):
            _LOGGER.debug("%s control error %s", self._client.address, raw.hex())

    def _on_data(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._log(f"<< data {bytes(data).hex()}")
        self._rx.extend(data)

    def _on_last_data(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._rx.extend(data)
        chunk = bytes(self._rx)
        self._rx.clear()
        self._log(f"<< chunk {chunk.hex()}")
        self._rx_since_grant += 1
        if self._rx_since_grant >= p.REGRANT_AFTER:
            grant = self._rx_since_grant
            self._rx_since_grant = 0
            task = asyncio.get_running_loop().create_task(
                self._write(p.CONTROL, bytes([p.CTRL_GRANT, grant]))
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        try:
            msgs = p.split_messages(p.cbor_decode_seq(chunk))
        except p.CborError as err:
            self._log(f"undecodable chunk: {err}")
            return
        for msg in msgs:
            self._log(f"   msg {msg!r}")
            if msg[0] == p.OP_VALUE and len(msg) >= 4 and isinstance(msg[3], bytes):
                self.latest.setdefault(msg[1], {})[msg[2]] = msg[3]
        self.messages.extend(msgs)
        self._message_event.set()

    async def wait_for(
        self, done: Callable[[list[list[Any]]], bool], timeout: float = REPLY_TIMEOUT
    ) -> bool:
        """Wait until ``done(messages)`` is true; False on timeout."""
        try:
            async with asyncio.timeout(timeout):
                while not done(self.messages):
                    self._message_event.clear()
                    await self._message_event.wait()
        except TimeoutError:
            return False
        return True

    # -- requests ---------------------------------------------------------

    async def get_devices(self) -> list[tuple[int, int]]:
        start = len(self.messages)
        await self.send(p.get_devices())

        def done(msgs: list[list[Any]]) -> bool:
            return any(m[0] == p.OP_DEVICE_LIST for m in msgs[start:])

        await self.wait_for(done, timeout=4)
        for msg in self.messages[start:]:
            if msg[0] == p.OP_DEVICE_LIST and len(msg) > 1 and isinstance(msg[1], list):
                flat = msg[1]
                return list(zip(flat[0::2], flat[1::2], strict=False))
        return []

    async def find_charger(self, devices: list[tuple[int, int]], probe_vregs: list[int]) -> int | None:
        """Return the instance that answers charger registers.

        A SmartSolar lists its Bluetooth module (0), the module's UART bridge
        (1) and the charger itself (3, type 1); try type-1 entries first.
        Leaves the returned instance subscribed.
        """
        order = [i for i, t in devices if t == 1] + [i for i, t in devices if t != 1]
        for instance in order or [0]:
            # the device ignores GetValues for an instance until it is subscribed
            await self.subscribe(instance)
            values, _errors = await self.get_values(instance, probe_vregs, timeout=4)
            if values:
                return instance
        return None

    async def set_value(
        self, instance: int, vreg: int, raw: bytes, timeout: float = REPLY_TIMEOUT
    ) -> bytes:
        """Write a register; return the value the charger echoes back.

        The charger answers a write with a Value message carrying the
        register's new contents (verified on SmartSolar 250/100 and 150/45),
        or a ValueResponse with an error code if it refuses.
        """
        start = len(self.messages)
        await self.send(p.set_value(instance, vreg, raw))

        def answered(msgs: list[list[Any]]) -> bool:
            return any(
                m[0] in (p.OP_VALUE, p.OP_VALUE_RESPONSE) and len(m) >= 4 and m[1] == instance and m[2] == vreg
                for m in msgs[start:]
            )

        if not await self.wait_for(answered, timeout):
            raise VictronSmartError(f"no answer to the write of 0x{vreg:04X}")
        for msg in self.messages[start:]:
            if len(msg) >= 4 and msg[1] == instance and msg[2] == vreg:
                if msg[0] == p.OP_VALUE_RESPONSE:
                    raise VictronSmartError(f"charger refused 0x{vreg:04X} (code {msg[3]})")
                if msg[0] == p.OP_VALUE:
                    return bytes(msg[3])
        raise VictronSmartError(f"no value echoed for 0x{vreg:04X}")  # pragma: no cover

    async def keep_alive(self) -> None:
        await self.send(p.keep_alive())

    async def subscribe(self, instance: int) -> None:
        await self.send(p.subscribe(instance))

    async def get_values(
        self, instance: int, vregs: Iterable[int], timeout: float = REPLY_TIMEOUT
    ) -> tuple[dict[int, bytes], dict[int, Any]]:
        """Read registers; returns (values, errors) keyed by vreg."""
        assert self.info is not None
        wanted = list(dict.fromkeys(vregs))
        start = len(self.messages)
        # opcode + instance + array head take at most 6 bytes, a vreg 3
        per_chunk = max(1, (max(self.info.chunk_size, 12) - 6) // 3)
        for i in range(0, len(wanted), per_chunk):
            await self.send(p.get_values(instance, wanted[i : i + per_chunk]))

        def answered(msgs: list[list[Any]]) -> set[int]:
            return {
                m[2]
                for m in msgs[start:]
                if m[0] in (p.OP_VALUE, p.OP_VALUE_RESPONSE) and len(m) >= 4 and m[1] == instance
            }

        await self.wait_for(lambda msgs: answered(msgs) >= set(wanted), timeout)
        values: dict[int, bytes] = {}
        errors: dict[int, Any] = {}
        for msg in self.messages[start:]:
            if len(msg) < 4 or msg[1] != instance:
                continue
            if msg[0] == p.OP_VALUE and isinstance(msg[3], bytes):
                values[msg[2]] = msg[3]
            elif msg[0] == p.OP_VALUE_RESPONSE:
                errors[msg[2]] = msg[3]
        for vreg in wanted:
            if vreg not in values and vreg not in errors:
                errors[vreg] = "timeout"
        return values, errors
