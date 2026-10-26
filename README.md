# Baby Tracker (Nighp) for Home Assistant

A custom integration for the Nighp "Baby Tracker" app. Home Assistant signs in to your account's
sync service and shows each baby's last diaper, feed, pump, and sleep.

This version only reads. Writing records, timers, and today's counts come in later versions. See
`PROTOCOL.md` for how the sync service works and `GLOSSARY.md` for the terms used here.

## What you get

- One Home Assistant device per baby.
- Four timestamp sensors on each baby: **Last diaper**, **Last feed**, **Last pump**, and
  **Last sleep**. The state is when it happened. The details are attributes:
  - Diaper: `type` (`wet`, `dirty`, or `mixed`) and `note`.
  - Feed: `kind` (`nursing`, `formula`, or `pumped`), `amount` and `unit` (`oz` or `mL`) for
    bottles, nursing durations in minutes, `finish_side`, and `note`. The newest of nursing,
    formula, and pumped milk counts as the last feed.
  - Pump: `amount`, `unit`, `left_amount`, `right_amount`, durations in minutes, `sides`, and
    `note`.
  - Sleep: `duration_minutes` (0 while a sleep is in progress) and `note`.
- Babies that appear later in your account are added without a restart.

## Installation

The integration isn't in HACS yet, so install it by copying:

1. Copy the `custom_components/nighp_baby_tracker` folder from this repository into your Home
   Assistant configuration folder, so you have `<config>/custom_components/nighp_baby_tracker/`.
2. Restart Home Assistant.

The library that talks to the service is bundled inside the integration (in `pybabytracker/`), so
there is nothing else to install.

## Configuration

1. Go to **Settings > Devices & services > Add integration** and choose **Baby Tracker (Nighp)**.
2. Enter the email and password you use in the app. The login is checked before anything is saved.

On first setup, Home Assistant loads your account's full history in one go. It saves what it has
seen, so a restart fetches only what's new.

Home Assistant signs in as its own device named "Home Assistant" and never borrows a phone's
identity. It doesn't appear in the app's device list until a later version writes its first record.

### Options

**Sync interval** sets how often Home Assistant checks for new records. The default is 5 minutes
and the minimum is 1 minute. Changing it takes effect without a reload.

### If your password changes

Home Assistant asks you to sign in again. Enter the account's current password. If you enter a
different account's email, the sign-in is refused.

## Removal

1. Go to **Settings > Devices & services**, open **Baby Tracker (Nighp)**, and choose **Delete**.
   This also deletes the saved sync state.
2. Delete the `custom_components/nighp_baby_tracker` folder and restart Home Assistant.

## Development

```sh
uv venv --python 3.13 .venv
uv pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check .
```

The library is vendored while it's private. To update the bundled copy, run
`scripts/vendor_library.sh <path-to-pybabytracker>`. It copies the package and writes the source
commit to `custom_components/nighp_baby_tracker/pybabytracker/VENDORED_FROM`. The vendored copy is
excluded from this repository's lint rules, because it's checked in the library's own repository.
