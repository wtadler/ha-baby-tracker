"""Constants for Baby Tracker (Nighp)."""

from datetime import timedelta

DOMAIN = "nighp_baby_tracker"

# The config entry's version: the minor number goes up when setup needs a one-time change.
CONFIG_ENTRY_VERSION = 1
CONFIG_ENTRY_MINOR_VERSION = 2

CONF_DEVICE_UUID = "device_uuid"

# How often the account syncs. Fixed: Home Assistant's polling rules leave it to the user to
# poll faster with ``homeassistant.update_entity``. An earlier version saved a ``scan_interval``
# option; it is ignored, and the next options save drops it.
SCAN_INTERVAL = timedelta(minutes=5)

# A nursing timer is Home Assistant's alone (the app doesn't sync a running one), so a forgotten one
# can't be seen or ended anywhere else. Once it has run this long it raises a repair and stops
# counting toward the one-minute polling. Sleeps are synced, so they have no such limit.
NURSING_TIMER_GUARD_AFTER = timedelta(minutes=60)

# How Home Assistant names itself in the account's device list.
DEVICE_NAME = "Home Assistant"

STORAGE_VERSION = 1

# The account-level device, one per config entry: it holds what belongs to the account rather than
# to a baby (the last pump). Its identifier can't clash with a baby's, which is a UUID.
ACCOUNT_DEVICE_MODEL = "Account"
# The device's name is the "account" device string ("Baby Tracker account"): fixed, so the
# account's email never ends up in an entity ID. A second entry gets "_2".
ACCOUNT_SUFFIX = "_account"


def nursing_guard_issue_id(entry_id: str, baby_id: str) -> str:
    """Return the repair issue ID for a baby's overlong nursing timer."""
    return f"nursing_timer_long_{entry_id}_{baby_id}"


def account_identifier(entry_id: str) -> str:
    """Return the identifier of an account's device."""
    return f"{entry_id}{ACCOUNT_SUFFIX}"


def is_account_identifier(identifier: str) -> bool:
    """Return whether an identifier belongs to an account's device."""
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
SERVICE_DISCARD_NURSING = "discard_nursing"

CONF_BOTTLE_SIZE = "bottle_size"
CONF_BOTTLE_UNIT = "bottle_unit"
CONF_BOTTLE_TYPE = "bottle_type"
