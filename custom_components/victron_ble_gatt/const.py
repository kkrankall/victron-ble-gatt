"""Constants for Victron BLE (GATT)."""

DOMAIN = "victron_ble_gatt"
MANUFACTURER = "Victron Energy"

CONF_SCAN_INTERVAL = "scan_interval"
CONF_PAIR = "pair"
CONF_ALLOW_CONTROL = "allow_control"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 15
HISTORY_REFRESH_SECONDS = 3600
# Settings live in the charger's non-volatile memory; refuse rapid-fire rewrites.
MIN_WRITE_INTERVAL = 10
