# Baby Tracker (Nighp) for Home Assistant

A custom integration for the Nighp "Baby Tracker" app. Home Assistant signs in to your account's
sync service and shows each baby's last diaper, feed, pump, and sleep.

This version reads, and logs diapers. Timers and today's counts come in later versions. See
`PROTOCOL.md` for how the sync service works and `GLOSSARY.md` for the terms used here.

## What you get

- One Home Assistant device per baby.
- Four timestamp sensors on each baby: **Last diaper**, **Last feed**, **Last pump**, and
  **Last sleep**. The state is when it happened. The details are attributes:
  - Diaper: `type` (`wet`, `dirty`, or `mixed`) and `note`.
  - Feed: `kind` (`nursing`, `formula`, or `pumped`), `amount` and `unit` (`fl. oz.` or `mL`) for
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

**Bottle size**, **Bottle unit**, and **Bottle type** set what each baby's **Bottle** button logs.
They start at 4 fl. oz. of formula when your Home Assistant uses US customary units, and 120 mL of
formula otherwise. A change applies to the next press, with no reload. Options that don't make
sense fall back to those defaults.

### If your password changes

Home Assistant asks you to sign in again. Enter the account's current password. If you enter a
different account's email, the sign-in is refused.

## Buttons and actions

Each baby has **Wet diaper**, **Dirty diaper**, **Mixed diaper**, and **Bottle** buttons.
Pressing one logs that diaper, or the bottle your options describe, for that baby, right now, as
Home Assistant's own device in your account.

The actions do the same and more:

- `nighp_baby_tracker.log_diaper` takes `baby` (the baby's device), `type` (`wet`, `dirty`, or
  `mixed`), an optional `time` (default now; a time with no offset is read in Home Assistant's time
  zone), and an optional `note`.
- `nighp_baby_tracker.log_bottle` takes `baby`, `kind` (`formula` or `pumped`), `amount` (more than
  zero), an optional `unit` (`fl. oz.` or `mL`; default `fl. oz.` for US customary units, `mL`
  otherwise), an optional `time`, and an optional `note`.
- `nighp_baby_tracker.log_nursing` takes `baby`, `left_minutes` and `right_minutes` (whole
  minutes; at least one above zero), an optional `finish_side` (`left` or `right`; default the last
  side used, right when both), an optional `end_time` (default now), and an optional `note`. The
  record starts at the end time minus the total minutes.
- `nighp_baby_tracker.log_sleep` takes `baby`, `start` and `end` (the end must be after the start),
  and an optional `note`. The length is saved in whole minutes, rounded half up, and never less
  than 1.
- `nighp_baby_tracker.log_pump` takes `baby`, `left_amount` and/or `right_amount`, an optional
  `unit`, `left_minutes` and `right_minutes`, an optional `finish_side`, an optional `time` (the
  start; default now), and an optional `note`. The total is the sum of the two sides.
- `nighp_baby_tracker.undo` deletes a record Home Assistant created. Give it `baby` or
  `config_entry_id` (the account), and optionally an `object_id`. Without one, it deletes the most
  recent record Home Assistant created (for that baby, if you gave `baby`). Run it again and it
  deletes the next older Home Assistant entry, and so on, until none are left. If you give both
  `baby` and `object_id`, the record must belong to that baby. It can't delete records the phones
  made.
- Every `log_*` action also takes an optional `object_id` (a UUID; reuse the same one to retry safely).
  Send the same one to retry a write that may have landed: it's added once, never twice.

Each write syncs first, then posts, and the sensors update at once. If a write gets no answer from
the service, the next one says "the last entry is still being confirmed"; try again in a minute. If
Home Assistant's own log in your account can't be read back, writing pauses and a repair appears in
**Settings > System > Repairs**. A rejected login starts the sign-in flow again.

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
