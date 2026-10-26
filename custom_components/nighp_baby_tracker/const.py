"""Constants for Baby Tracker (Nighp)."""

DOMAIN = "nighp_baby_tracker"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_DEVICE_UUID = "device_uuid"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL_MINUTES = 5
MIN_SCAN_INTERVAL_MINUTES = 1
MAX_SCAN_INTERVAL_MINUTES = 1440

# How Home Assistant names itself in the account's device list.
DEVICE_NAME = "Home Assistant"

STORAGE_VERSION = 1

SERVICE_LOG_DIAPER = "log_diaper"
SERVICE_LOG_BOTTLE = "log_bottle"
SERVICE_LOG_NURSING = "log_nursing"
SERVICE_LOG_SLEEP = "log_sleep"
SERVICE_LOG_PUMP = "log_pump"
SERVICE_UNDO = "undo"

CONF_BOTTLE_SIZE = "bottle_size"
CONF_BOTTLE_UNIT = "bottle_unit"
CONF_BOTTLE_TYPE = "bottle_type"
