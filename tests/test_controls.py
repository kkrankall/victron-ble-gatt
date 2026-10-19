"""Charger on/off and charge current limit against the simulated MPPT."""

import struct
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.victron_ble_gatt.const import CONF_ALLOW_CONTROL, DOMAIN

from .conftest import ADDRESS, inject, service_info

SWITCH = "switch.array1_charging"
NUMBER = "number.array1_charge_current_limit"


async def _setup(hass: HomeAssistant, allow: bool) -> MockConfigEntry:
    inject(hass, service_info(manufacturer_id=None))
    await hass.async_block_till_done()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Array1",
        data={"address": ADDRESS},
        options={CONF_ALLOW_CONTROL: allow},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_no_controls_unless_allowed(hass: HomeAssistant, enable_bluetooth, mppt):
    await _setup(hass, allow=False)
    assert hass.states.get(SWITCH) is None
    assert hass.states.get(NUMBER) is None


async def test_charging_switch(hass: HomeAssistant, enable_bluetooth, mppt):
    await _setup(hass, allow=True)
    assert hass.states.get(SWITCH).state == "on"
    await hass.services.async_call("switch", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: SWITCH}, blocking=True)
    assert mppt.registers[0x0200] == bytes([4])
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get("sensor.array1_charger_mode").state == "off"
    # turning back on right away is never blocked
    await hass.services.async_call("switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: SWITCH}, blocking=True)
    assert mppt.registers[0x0200] == bytes([1])
    assert hass.states.get(SWITCH).state == "on"


async def test_current_limit(hass: HomeAssistant, enable_bluetooth, mppt):
    await _setup(hass, allow=True)
    state = hass.states.get(NUMBER)
    assert state.state == "70.0"
    assert state.attributes["max"] == 70.0  # rated current of the simulated charger
    await hass.services.async_call(
        "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: NUMBER, ATTR_VALUE: 35}, blocking=True
    )
    assert mppt.registers[0xEDF0] == struct.pack("<H", 350)
    assert hass.states.get(NUMBER).state == "35.0"
    # the same value again is not written (settings memory wear)
    writes = len(mppt.writes)
    await hass.services.async_call(
        "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: NUMBER, ATTR_VALUE: 35}, blocking=True
    )
    assert len(mppt.writes) == writes
    # a different value straight away is refused
    with pytest.raises(HomeAssistantError, match="wait"):
        await hass.services.async_call(
            "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: NUMBER, ATTR_VALUE: 30}, blocking=True
        )
    assert mppt.registers[0xEDF0] == struct.pack("<H", 350)


async def test_refused_write(hass: HomeAssistant, enable_bluetooth, mppt):
    await _setup(hass, allow=True)
    mppt.refuse.add(0xEDF0)
    with pytest.raises(HomeAssistantError, match="refused"):
        await hass.services.async_call(
            "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: NUMBER, ATTR_VALUE: 20}, blocking=True
        )
    assert hass.states.get(NUMBER).state == "70.0"
