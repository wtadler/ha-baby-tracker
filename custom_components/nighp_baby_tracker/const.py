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

# The account-level device, one per config entry: it holds what belongs to the account rather than
# to a baby (the last pump). Its identifier can't clash with a baby's, which is a UUID.
ACCOUNT_DEVICE_MODEL = "Account"
# A fixed name, so the account's email never ends up in an entity ID. A second entry gets "_2".
ACCOUNT_DEVICE_NAME = "Baby Tracker account"
ACCOUNT_SUFFIX = "_account"


def account_identifier(entry_id: str) -> str:
    return f"{entry_id}{ACCOUNT_SUFFIX}"


def is_account_identifier(identifier: str) -> bool:
    return identifier.endswith(ACCOUNT_SUFFIX)


SERVICE_LOG_DIAPER = "log_diaper"
SERVICE_LOG_BOTTLE = "log_bottle"
SERVICE_LOG_NURSING = "log_nursing"
SERVICE_LOG_SLEEP = "log_sleep"
SERVICE_LOG_PUMP = "log_pump"
SERVICE_UNDO = "undo"
SERVICE_START_SLEEP = "start_sleep"
SERVICE_STOP_SLEEP = "stop_sleep"
SERVICE_START_NURSING = "start_nursing"
SERVICE_SWITCH_NURSING_SIDE = "switch_nursing_side"
SERVICE_STOP_NURSING = "stop_nursing"

CONF_BOTTLE_SIZE = "bottle_size"
CONF_BOTTLE_UNIT = "bottle_unit"
CONF_BOTTLE_TYPE = "bottle_type"
