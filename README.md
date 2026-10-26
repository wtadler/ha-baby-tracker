# Baby Tracker (Nighp) for Home Assistant

A custom integration for the Nighp "Baby Tracker" app. Home Assistant signs in to your account's
sync service and shows each baby's last diaper, feed, pump, and sleep.

This version reads, logs diapers, bottles, nursing, sleep, and pumps, and runs sleep and nursing
timers. See
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
- An **Asleep** sensor, on while the baby's newest sleep is running, from any device.
- Two timer sensors on each baby, built for a live activity:
  - **Sleep timer**: when the running sleep started (from any device), or unknown. Attribute
    `object_id`.
  - **Nursing timer**: when Home Assistant's nursing timer started, or unknown. Attributes
    `side` (`left` or `right`), `side_started` (when the current side began), and
    `left_seconds` and `right_seconds` (time on each side before the current one). The current
    side's time is now minus `side_started`.
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
They start at 4 fl. oz. of formula in every unit system. Metric isn't supported yet: the app's
metric format hasn't been confirmed, so a bottle or pump in mL is refused with an error and the
button does the same if its unit is mL. Today's bottle volume still shows in your unit system.
A change applies to the next press, with no reload. Options that don't make
sense fall back to those defaults.

### If your password changes

Home Assistant asks you to sign in again. Enter the account's current password. If you enter a
different account's email, the sign-in is refused.

## Buttons and actions

Each baby has **Wet diaper**, **Dirty diaper**, **Mixed diaper**, and **Bottle** buttons.
Pressing one logs that diaper, or the bottle your options describe, for that baby, right now, as
Home Assistant's own device in your account.

### Timers

- **Sleep** starts a sleep, or ends the one running, wherever it started. The app syncs a running
  sleep, so the phones show it at once and Home Assistant sees theirs. A press does what Home
  Assistant showed when you pressed it: if a phone already ended that sleep, or already started
  one, the press writes nothing rather than starting a second sleep.
- **Nurse left** and **Nurse right** run Home Assistant's own nursing timer. Press a side to start
  it, the other side to switch (the first side's time is kept), and the running side again to stop.
  The app doesn't sync a running nursing timer, so Home Assistant can't see one running on a phone,
  and the phones see Home Assistant's only when it stops: one nursing record with each side's whole
  minutes (rounded half up), the side it ended on, and the start time. A stop within 30 seconds is
  saved as 1 minute on that side, and more than 240 minutes on a side is saved as 240. The timer
  survives a restart, and nursing while the baby is asleep is fine.
- While any sleep or nursing timer runs, Home Assistant syncs every minute, so a phone's stop shows
  up quickly. It returns to your sync interval when none is running.
- A sleep can't be longer than 24 hours. Stopping one later than that saves 24 hours, so a
  forgotten timer can always be stopped; fix the length in the app.

The actions do the same and more:

- `nighp_baby_tracker.log_diaper` takes `baby` (the baby's device), `type` (`wet`, `dirty`, or
  `mixed`), an optional `time` (default now; a time with no offset is read in Home Assistant's time
  zone), and an optional `note`.
- `nighp_baby_tracker.log_bottle` takes `baby`, `kind` (`formula` or `pumped`), `amount` (more than
  zero), an optional `unit` (`fl. oz.`; `mL` is refused for now), an optional `time`, and an optional `note`.
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
- `nighp_baby_tracker.start_sleep` takes `baby`, an optional `start` (default now; not in the
  future or more than 24 hours ago), an optional `note`, and an optional `object_id`. It's refused
  while the baby's newest sleep is running.
- `nighp_baby_tracker.stop_sleep` takes `baby` and an optional `end` (default now). It ends the
  baby's running sleep, whether Home Assistant or a phone started it, and is refused when none is
  running.
- `nighp_baby_tracker.start_nursing` takes `baby` and `side` (`left` or `right`). With the timer on
  the other side, it switches; on the same side, nothing changes.
- `nighp_baby_tracker.switch_nursing_side` takes `baby` and moves the timer to the other side.
- `nighp_baby_tracker.stop_nursing` takes `baby` and an optional `note`, and saves the nursing
  record. Switching or stopping with no timer running is refused.
- Every `log_*` action, and `start_sleep`, also takes an optional `object_id` (a UUID; reuse the same one to retry safely).
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
