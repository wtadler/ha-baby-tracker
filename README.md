# Baby Tracker (Nighp) for Home Assistant

A custom integration for the Nighp "Baby Tracker" app. Home Assistant signs in to your account's
sync service and shows each baby's last diaper, feed, pump, and sleep.

This version reads, logs diapers, bottles, nursing, sleep, and pumps, and runs sleep and nursing
timers. See
`PROTOCOL.md` for how the sync service works and `GLOSSARY.md` for the terms used here.

## What you get

- One Home Assistant device per baby, and one account device per config entry ("Baby Tracker account", model
  "Account"; a second entry's sensor ends `_2`).
- Three timestamp sensors on each baby: **Last diaper**, **Last feed**, and **Last sleep**. The
  state is when it happened. The details are attributes:
  - Diaper: `type` (`wet`, `dirty`, or `mixed`) and `note`.
  - Feed: `kind` (`nursing`, `formula`, or `pumped`), `amount` and `unit` (`fl. oz.` or `mL`) for
    bottles, nursing durations in minutes, `finish_side`, and `note`. The newest of nursing,
    formula, and pumped milk counts as the last feed.
  - Sleep: `duration_minutes` (0 while a sleep is in progress) and `note`.
- **Last pump**, on the account device: the newest pump on the account, whoever logged it. Pumps
  belong to the account, not a baby (the app stores no baby with them), so this isn't on a baby's
  device. Attributes: `amount`, `unit`, `left_amount` and `right_amount` (only when the sides were
  entered), durations in minutes, `sides`, and `note`. Upgrading removes the old per-baby last-pump
  sensors from the entity registry; update any dashboard or automation that used one.
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

There is no sync interval option. Home Assistant checks for new records every 5 minutes, and every
minute while a sleep or nursing timer runs. To check sooner, call `homeassistant.update_entity`
on any Baby Tracker entity from an automation or a script. An earlier version's saved interval is
ignored, and saving these options removes it.

**Bottle size**, **Bottle unit**, and **Bottle type** set what each baby's **Bottle** button logs.
They start at 4 fl. oz. of formula in every unit system. Metric isn't supported yet: the app's
metric format hasn't been confirmed, so a bottle or pump in mL is refused with an error and the
button does the same if its unit is mL. Today's bottle volume still shows in your unit system.
A change applies to the next press, with no reload. Options that don't make
sense fall back to those defaults.

### If your password changes

Home Assistant asks you to sign in again. Enter the account's current password. The email is the
one the entry was set up with, and it isn't asked again.

## Buttons and actions

Each baby has **Wet diaper**, **Dirty diaper**, **Mixed diaper**, and **Bottle** buttons.
Pressing one logs that diaper, or the bottle your options describe, for that baby, right now, as
Home Assistant's own device in your account.

### Timers

- **Sleep** starts a sleep, or ends the one running, wherever it started. The app syncs a running
  sleep, so the phones show it at once and Home Assistant sees theirs. A press does what Home
  Assistant showed when you pressed it: if a phone already ended that sleep, or already started
  one, the press writes nothing rather than starting a second sleep, and shows an error saying so.
- **Nurse left** and **Nurse right** run Home Assistant's own nursing timer. Press a side to start
  it, the other side to switch (the first side's time is kept), and the running side again to stop.
  The app doesn't sync a running nursing timer, so Home Assistant can't see one running on a phone,
  and the phones see Home Assistant's only when it stops: one nursing record with each side's whole
  minutes (rounded half up), the side it ended on, and the start time. A stop within 30 seconds is
  saved as 1 minute on that side, and more than 240 minutes on a side is saved as 240. The timer
  survives a restart (if it can't be saved, the press still works and shows an error), and nursing
  while the baby is asleep is fine. A timer for a baby no longer in the account is dropped.
- While any sleep or nursing timer runs, Home Assistant syncs every minute, so a phone's stop shows
  up quickly. It returns to every 5 minutes when none is running.
- A sleep can't be longer than 24 hours. Stopping one later than that saves 24 hours, so a
  forgotten timer can always be stopped; fix the length in the app.

The actions do the same and more. A baby action takes the baby's device as `device_id`. The
account actions (`log_pump`, and `undo` for a whole account) take `config_entry_id`.

**Upgrading automations:** the old `baby` field (and `device` on `log_pump`) still works for one
more release. Using it raises a repair in **Settings > System > Repairs** that names the action
and the field to switch to: `device_id` for a baby, and `config_entry_id` for an account.


- `nighp_baby_tracker.log_diaper` takes `device_id` (the baby's device), `type` (`wet`, `dirty`, or
  `mixed`), an optional `time` (default now; a time with no offset is read in Home Assistant's time
  zone), and an optional `note`.
- `nighp_baby_tracker.log_bottle` takes `device_id`, `kind` (`formula` or `pumped`), `amount` (more than
  zero), an optional `unit` (`fl. oz.`; `mL` is refused for now), an optional `time`, and an optional `note`.
- `nighp_baby_tracker.log_nursing` takes `device_id`, `left_minutes` and `right_minutes` (whole
  minutes; at least one above zero), an optional `finish_side` (`left` or `right`; default the last
  side used, right when both), an optional `end_time` (default now), and an optional `note`. The
  record starts at the end time minus the total minutes.
- `nighp_baby_tracker.log_sleep` takes `device_id`, `start` and `end` (the end must be after the start),
  and an optional `note`. The length is saved in whole minutes, rounded half up, and never less
  than 1.
- `nighp_baby_tracker.log_pump` takes `config_entry_id` (the account), `left_amount` and/or
  `right_amount`, an optional `unit`, `left_minutes` and `right_minutes`, an optional
  `finish_side`, an optional `time` (the start; default now), and an optional `note`. The total is
  the sum of the two sides. A pump belongs to the account, so no baby is saved with it.
- `nighp_baby_tracker.undo` deletes a record Home Assistant created. Give it `device_id` (a baby) or
  `config_entry_id` (the account), and optionally an `object_id`. Without one, it deletes the most
  recent record Home Assistant created (for that baby, if you gave `device_id`). Run it again and it
  deletes the next older Home Assistant entry, and so on, until none are left. If you give both
  `device_id` and `object_id`, the record must belong to that baby. It can't delete records the phones
  made.
- `nighp_baby_tracker.start_sleep` takes `device_id`, an optional `start` (default now; not in the
  future or more than 24 hours ago), an optional `note`, and an optional `object_id`. It's refused
  while the baby's newest sleep is running.
- `nighp_baby_tracker.stop_sleep` takes `device_id` and an optional `end` (default now). It ends the
  baby's running sleep, whether Home Assistant or a phone started it, and is refused when none is
  running.
- `nighp_baby_tracker.start_nursing` takes `device_id` and `side` (`left` or `right`). With the timer on
  the other side, it switches; on the same side, nothing changes.
- `nighp_baby_tracker.switch_nursing_side` takes `device_id` and moves the timer to the other side.
- `nighp_baby_tracker.stop_nursing` takes `device_id` and an optional `note`, and saves the nursing
  record. Switching or stopping with no timer running is refused.
- Every `log_*` action, and `start_sleep`, also takes an optional `object_id` (a UUID; reuse the same one to retry safely).
  Send the same one to retry a write that may have landed: it's added once, never twice.

Each write syncs first, then posts, and the sensors update at once. If a write gets no answer from
the service, the next one says "the last entry is still being confirmed"; try again in a minute. If
Home Assistant's own log in your account can't be read back, writing pauses. If that lasts more
than an hour, a repair appears in **Settings > System > Repairs** with the steps to fix it; a
shorter gap raises an error on the write and no repair. A rejected login starts the sign-in flow again.

## Live activity on your iPhone

The blueprint `blueprints/automation/nighp_baby_tracker/live_activity.yaml` turns a baby's running
timers into iOS Live Activities through the Home Assistant Companion app. The integration itself
sends no notifications; the blueprint does, from the timer sensors.

**Import it**

1. Copy the file to `<config>/blueprints/automation/nighp_baby_tracker/live_activity.yaml`, or
   open **Settings > Automations & scenes > Blueprints > Import blueprint** and paste its GitHub
   URL: `https://github.com/wtadler/ha-baby-tracker/blob/main/blueprints/automation/nighp_baby_tracker/live_activity.yaml`
2. Create an automation from it, one per baby, and fill in:
   - **Sleep timer** and **Nursing timer**: that baby's two timer sensors (the pickers list only
     this integration's timestamp sensors).
   - **Notify service**: the Companion app's action for the phone you want the activity on, such as
     `notify.mobile_app_your_phone`. Find the name in the app under **Settings > Companion app >
     Server & devices**, or try it in **Developer tools > Actions** first. For several phones,
     create one automation per phone.
   - **Sleep title** and **Nursing title** (optional): the activity's header.

**What shows on the lock screen**

- A sleep timer starting shows "Asleep" under the title, with a running count-up from the time the
  sleep began (a sleep a phone started shows its real start time). The timer stopping ends the
  activity.
- A nursing timer starting shows "Nursing · Left side" (or right) with a count-up from the start of
  the session. Switching sides updates the same activity to the new side. Stopping ends it.
- Sleep and nursing are separate activities with their own tags (`sleep_<sensor name>` and
  `nursing_<sensor name>`), so a baby can show both, and each baby has their own.

**Limits**

- It needs the Home Assistant iOS Companion app with Live Activity support (iOS 17.2 or later)
  and Home Assistant 2026.7.0 or later, as the
  [Companion app docs](https://companion.home-assistant.io/docs/notifications/live-activities)
  require. The app and Home Assistant must be able to reach each other to sync a token, so it
  may not update while the phone has no connection to Home Assistant.
- The count-up runs from the session's start, so it keeps counting through a nursing side switch
  instead of restarting. The notification's title can't change once the activity starts, so the
  side is in the message, not the title.
- A nursing timer started in the phone app can't show: the app doesn't sync a running nursing
  timer, so Home Assistant never sees it. Only timers started from Home Assistant (the **Nurse
  left** and **Nurse right** buttons or actions) do. Sleep timers show from any device.
- If Home Assistant restarts mid-session, a running timer's activity stays as it was and the
  blueprint sends nothing new until the timer changes; ending it still ends the activity.
- A sync failure mid-timer (running, then unavailable, then unknown) still ends the activity; going unavailable and back to the same running timer leaves it alone.
- The blueprint runs queued, so a start, a switch, and an end are sent in order.

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
