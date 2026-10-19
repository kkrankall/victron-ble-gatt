# Victron BLE (GATT) for Home Assistant

Reads **every value VictronConnect shows** for a Victron SmartSolar MPPT
solar charger over Bluetooth: live PV and battery readings, charger state,
yields, today's and yesterday's history, lifetime records, the 30-day history
and the main charge settings. Local only, no VRM or GX device needed.

Home Assistant's built-in **Victron BLE** integration only decodes the
"Instant readout" advertisement (8 values). This one connects to the charger
the way VictronConnect does. It uses its own domain (`victron_ble_gatt`), so
both can run side by side.

Tested on a SmartSolar MPPT VE.Can 250/100 (firmware 3.18) and a SmartSolar
MPPT 150/45 (firmware 1.68) through an ESPHome Bluetooth proxy.

Not affiliated with or endorsed by Victron Energy. The Victron Energy name and
logo (the integration icon, the same images Home Assistant uses for its own
Victron BLE integration) are trademarks of Victron Energy B.V., used here only
to identify the devices this integration works with.

## What you get

One device per charger, with sensors for the registers the charger supports:

| Group | Sensors |
| --- | --- |
| Solar | PV power, PV voltage, PV current, tracker mode; per-tracker voltage/current/power on multi-tracker models |
| Battery | Charge voltage, charge current, battery temperature (with a temperature sense), charger temperature |
| State | Charger state (bulk, absorption, float, ...), charger mode, error, off reason, load output, relay |
| Yield | Solar yield today / yesterday / total / system, max power today / yesterday |
| History | Today's and yesterday's time in bulk, absorption and float, min/max battery voltage, max battery current, max PV voltage |
| Lifetime | Max PV voltage, min/max battery voltage, last errors |
| Daily history | One sensor whose `days` attribute holds the last 31 daily records (yield, max power, times, voltages, errors) |
| Settings (diagnostic) | Battery voltage, charge current limit, absorption and float voltage, rated charge current, uptime |
| Controls (opt-in) | Charging on/off switch, charge current limit (see below) |

Every reading opens a short connection (about 3–4 s), every 60 seconds by
default (**Configure** to change it). The 30-day history is refreshed hourly.
While a reading is in progress, VictronConnect on a phone cannot connect, and
the other way round.

## Controls (opt-in)

Turn on **Allow control** under **Configure** to add two entities per charger:

| Entity | Writes | Notes |
| --- | --- | --- |
| `switch.<charger>_charging` | Device mode (`0x0200`): on = 1, off = 4 | Off stops charging; the charger then reports "soft power switch" as its off reason. |
| `number.<charger>_charge_current_limit` | Max charge current (`0xEDF0`), 1 A up to the charger's rating | The same setting as VictronConnect's charge current slider. |

Every write is confirmed: the charger echoes the register's new value and
the integration checks it, so a refused or ignored write shows an error
instead of a stale state.

Both are the charger's **saved settings**, not temporary overrides. They
stay in effect through power cycles and if Home Assistant goes offline, so
an automation that throttles charging should also restore the limit. To
spare the charger's settings memory, writing the value it already has is
skipped and the current limit can change at most once every 10 seconds.
A Victron GX with DVCC can override the current limit.

## Requirements

- Home Assistant 2026.10 or newer.
- A Bluetooth adapter or **ESPHome Bluetooth proxy** in range of the charger,
  with active connections (`bluetooth_proxy: active: true`).
- **PIN pairing.** Victron chargers only allow access after Bluetooth pairing
  with the charger's PIN (factory default `000000`). An ESPHome proxy cannot
  ask Home Assistant for a PIN, so the proxy answers it itself. Add this to the
  proxy's YAML, one `ble_client` per charger, and reinstall it:

  ```yaml
  esp32_ble:
    io_capability: keyboard_only
    auth_req_mode: sc_mitm_bond
    max_connections: 5   # bluetooth_proxy slots (3) + one per charger

  ble_client:
    - mac_address: C0:FF:EE:00:00:01   # your charger
      id: charger1_pin
      auto_connect: false              # never connects by itself
      on_passkey_request:
        then:
          - ble_client.passkey_reply:
              id: charger1_pin
              passkey: !secret charger1_pin
  ```

  with `charger1_pin: "000000"` (quoted) in the ESPHome `secrets.yaml`. The
  `ble_client` never holds a connection; it only answers the PIN request when
  the proxy pairs on Home Assistant's behalf. After the first pairing the bond
  is stored on both sides.

  Adapters on the Home Assistant host itself have not been tested; they would
  need the charger bonded beforehand (for example with `bluetoothctl`).

## Install

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/kkrankall/victron-ble-gatt` as an **Integration**, then
   download **Victron BLE (GATT)**.
2. Restart Home Assistant.
3. Chargers are discovered automatically once a proxy hears them. Otherwise:
   **Settings → Devices & services → Add integration → Victron BLE (GATT)**.

## Troubleshooting

- *"VE.Smart service refused access ... Insufficient authentication"*: the
  charger was not paired. Check the PIN setup above and that **Pair** is on
  under **Configure**.
- The **`victron_ble_gatt.probe`** action (Developer tools → Actions, with
  "return response") connects to a configured charger, reads the registers
  you list (for example `[0xEDBB, 0xEDD3]`) and returns the raw exchange.
- **Download diagnostics** on the integration includes every raw register
  read.

## Protocol notes

Recovered by static analysis of VictronConnect 6.43 for macOS (the
`VeSmartService` and `VeService` classes, the embedded register catalog and
the solar charger screens) and verified on the hardware.

- **Service** `306b0001-b081-4037-83dc-e59fcc3cdfd0`. Characteristics are the
  service UUID with the first byte group incremented: `…0002` control,
  `…0003` final piece of a message, `…0004` continuation pieces. All three
  notify; the link must be encrypted (PIN pairing).
- **Handshake**: enable notifications (control, `…0003`, `…0004`), read
  control (`flags u8, max_instances u16, max_free_chunks u8, chunk_size u8,
  att_length u16`, little endian), write `FA 80 FF` to control, then
  `F9 80` to grant the charger 128 chunks. `F9 n` is the flow-control
  credit in both directions; the chargers grant one chunk at a time.
- **Messages** are CBOR sequences `opcode, params...`, several per chunk.
  A chunk longer than the ATT length is split: continuation pieces to
  `…0004`, the last piece to `…0003`.
  - Requests: `1` get device list, `3 inst` subscribe, `5 inst [vregs]`
    get values, `6 inst [vreg, bytes]` set value (answered with a value
    message echoing the register's new contents).
  - Replies: `2 [inst, type, ...]` device list (indefinite-length array),
    `7 x op result` response, `8 inst vreg bytes` value, `9 inst vreg err`
    value error.
- A SmartSolar lists its Bluetooth module (instance 0), the module's UART
  bridge (1) and the charger (3, type 1). The charger **ignores get-values
  until the instance is subscribed**; after subscribing it also pushes
  changed values.
- Values are the raw little-endian VE.Direct HEX registers (`0xEDBB` PV
  voltage in 0.01 V, `0xEDD3` yield today in 0.01 kWh, `0x1050`–`0x106E`
  daily history records, `0x104F` lifetime totals, ...).
