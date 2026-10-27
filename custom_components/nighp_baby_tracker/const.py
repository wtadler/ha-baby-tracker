"""Constants for Baby Tracker (Nighp)."""

from datetime import timedelta
from enum import StrEnum  # Core PR 9

DOMAIN = "nighp_baby_tracker"

# HACS-only: the config entry's minor version (1.2 is the migration in ``__init__.py``).
CONFIG_ENTRY_MINOR_VERSION = 2

CONF_DEVICE_UUID = "device_uuid"

# How often the account syncs. Fixed: Home Assistant's polling rules leave it to the user to
# poll faster with ``homeassistant.update_entity``.
SCAN_INTERVAL = timedelta(minutes=5)

# Core PR 18
# A nursing timer is Home Assistant's alone (the app doesn't sync a running one), so a forgotten one
# can't be seen or ended anywhere else. Once it has run this long it raises a repair and stops
# counting toward the one-minute polling. Sleeps are synced, so they have no such limit.
NURSING_TIMER_GUARD_AFTER = timedelta(minutes=60)
# Core PR 21
# The pump timer is Home Assistant's alone in the same way, so it has the same hour.
PUMP_TIMER_GUARD_AFTER = timedelta(minutes=60)

# How Home Assistant names itself in the account's device list.
DEVICE_NAME = "Home Assistant"

STORAGE_VERSION = 1

# Core PR 5
# The account-level device, one per config entry: it holds what belongs to the account rather than
# to a baby (the last pump). Its identifier can't clash with a baby's, which is a UUID.
ACCOUNT_DEVICE_MODEL = "Account"
# Core PR 5
# The device's name is the "account" device string ("Baby Tracker account"): fixed, so the
# account's email never ends up in an entity ID. A second entry gets "_2".
ACCOUNT_SUFFIX = "_account"


# Core PR 18
def nursing_guard_issue_id(entry_id: str, baby_id: str) -> str:
    """Return the repair issue ID for a baby's overlong nursing timer."""
    return f"nursing_timer_long_{entry_id}_{baby_id}"


# Core PR 21
def pump_guard_issue_id(entry_id: str) -> str:
    """Return the repair issue ID for an account's overlong pump timer."""
    return f"pump_timer_long_{entry_id}"


# Core PR 5
def account_identifier(entry_id: str) -> str:
    """Return the identifier of an account's device."""
    return f"{entry_id}{ACCOUNT_SUFFIX}"


# Core PR 5
def is_account_identifier(identifier: str) -> bool:
    """Return whether an identifier belongs to an account's device."""
    return identifier.endswith(ACCOUNT_SUFFIX)


CONF_BOTTLE_SIZE = "bottle_size"  # Core PR 15
CONF_BOTTLE_UNIT = "bottle_unit"  # Core PR 15
CONF_BOTTLE_TYPE = "bottle_type"  # Core PR 15


# Core PR 9
class BabyTrackerService(StrEnum):
    """The names of the integration's actions."""

    LOG_DIAPER = "log_diaper"
    LOG_BOTTLE = "log_bottle"
    LOG_NURSING = "log_nursing"
    LOG_SLEEP = "log_sleep"
    LOG_PUMP = "log_pump"
    START_PUMP = "start_pump"
    STOP_PUMP = "stop_pump"
    UNDO = "undo"
    START_SLEEP = "start_sleep"
    STOP_SLEEP = "stop_sleep"
    START_NURSING = "start_nursing"
    SWITCH_NURSING_SIDE = "switch_nursing_side"
    STOP_NURSING = "stop_nursing"
    DISCARD_NURSING = "discard_nursing"
