"""Discovery, setup, sensors and the probe action against the simulated MPPT."""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.victron_ble_gatt import protocol as p
from custom_components.victron_ble_gatt.const import DOMAIN

from .conftest import ADDRESS, inject, service_info


@pytest.fixture
async def setup(hass: HomeAssistant, enable_bluetooth, mppt):
    # history must be read on the first poll even right after the host booted
    patcher = patch(
        "custom_components.victron_ble_gatt.coordinator.time.monotonic", return_value=10.0
    )
    patcher.start()
    inject(hass, service_info(manufacturer_id=None))
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title="Array1", data={"address": ADDRESS})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    patcher.stop()


async def test_bluetooth_discovery(hass: HomeAssistant, enable_bluetooth):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Array1"


async def test_sensors(hass: HomeAssistant, setup, mppt):
    def state(key):
        return hass.states.get(f"sensor.array1_{key}").state

    assert state("pv_voltage") == "98.76"
    assert state("pv_power") == "513.55"
    assert state("charge_voltage") == "27.12"
    assert state("charge_current") == "18.6"
    assert state("charger_state") == "float"
    assert state("error") == "no_error"
    assert state("tracker_mode") == "tracking"
    assert state("solar_yield_today") == "4.12"
    assert state("solar_yield_total") == "2802.56"
    assert state("max_pv_voltage_lifetime") == "145.12"
    assert state("time_in_absorption_yesterday") == "95"
    assert state("battery_temperature") == "unknown"  # no sensor fitted
    assert hass.states.get("sensor.array1_load_current") is None  # not supported
    history = hass.states.get("sensor.array1_daily_history")
    assert history.state == "3"
    assert history.attributes["days"][1]["yield"] == 6.3
    # handshake as VictronConnect does it
    assert mppt.grants[:2] == [p.CTRL_HELLO, bytes([0xF9, 0x80])]
    assert mppt.requests[0] == [p.OP_GET_DEVICES]
    assert state("charger_mode") == "on"
    assert state("last_errors") == "None"
    assert hass.states.get("sensor.array1_last_errors").attributes["codes"] == []
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), setup.entry_id)
    assert device.sw_version == "3.18"
    assert device.model == "SmartSolar MPPT VE.Can 150/70"


async def test_probe(hass: HomeAssistant, setup):
    result = await hass.services.async_call(
        DOMAIN,
        "probe",
        {"address": ADDRESS, "vregs": ["0xEDBB", 0xEEEE], "listen": 0},
        blocking=True,
        return_response=True,
    )
    assert result["values"] == {"0xEDBB": "9426"}
    assert result["errors"] == {"0xEEEE": 1}
    assert [tuple(d) for d in result["devices"]] == [(0, 0), (1, 0), (3, 1)]
    assert result["instance"] == 3
