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
- **Pump timer**, also on the account device: a switch that is on while Home Assistant's pump timer
  runs, and a timestamp sensor with the same name (`sensor.baby_tracker_account_pump_timer`) that
  shows when it started, or is unknown. See [Timers](#timers).
- An **Asleep** sensor, on while the baby's newest sleep is running, from any device.
- Two timer sensors on each baby, built for a live activity:
  - **Sleep timer**: when the running sleep started (from any device), or unknown. Attribute
    `object_id`.
  - **Nursing timer**: when Home Assistant's nursing timer started, or unknown. Attributes
    `side` (`left` or `right`), `side_started` (when the current side began), and
    `left_seconds` and `right_seconds` (time on each side before the current one). The current
    side's time is now minus `side_started`.
- Timer controls on each baby (see [Timers](#timers)):
  - Switches: **Sleep**, **Nursing left**, and **Nursing right**. A switch is on while its timer
    runs.
  - Sensors **Nursing left time** and **Nursing right time**: the minutes on each side this
    session, counting up while it runs, or unknown when no session runs.
  - A **Discard nursing** button.
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
identity. It doesn't appear in the app's device list until it writes its first record.

### Options

There is no sync interval option. Home Assistant checks for new records every 5 minutes, and every
minute while a sleep timer or a nursing timer under an hour old runs. To check sooner, call `homeassistant.update_entity`
on any Baby Tracker entity from an automation or a script. An earlier version's saved interval is
ignored, and saving these options removes it.

**Bottle size**, **Bottle unit**, and **Bottle type** set what each baby's **Bottle** button logs.
They start at 120 mL of formula in a metric unit system and 4 fl. oz. of formula otherwise. A
saved choice is never changed. Today's bottle volume shows in your unit system. A change applies
to the next press, with no reload. Options that don't make sense fall back to those defaults.

Every record is written with the standard UTC offset of Home Assistant's time zone, with no
daylight saving, as the app does.

### If your password changes

Home Assistant asks you to sign in again. Enter the account's current password. The email is the
one the entry was set up with, and it isn't asked again.

## Buttons, switches, and actions

Each baby has **Wet diaper**, **Dirty diaper**, **Mixed diaper**, and **Bottle** buttons.
Pressing one logs that diaper, or the bottle your options describe, for that baby, right now, as
Home Assistant's own device in your account.

### Timers

The timers are switches, so you can see that one is running. They replace the Sleep and Nurse
buttons of earlier versions: upgrading removes those buttons from the entity registry, so update
any dashboard or automation that used one.

- **Sleep** is on while the baby's newest sleep is running, wherever it started. The app syncs a
  running sleep, so the phones show one started here at once and Home Assistant sees theirs. Turn
  it on to start a sleep, and off to end the running one, even one a phone started. These do what
  the `start_sleep` and `stop_sleep` actions do, errors included: turning it on while a sleep runs,
  or off when none does (a phone may have ended it since the switch last updated), raises an error
  and writes nothing.
- **Nursing left** and **Nursing right** work like the app's two side buttons and run Home
  Assistant's own nursing timer. A side is on while the timer runs on it, so at most one is on.
  - Turn a side on with no session running to start one on that side.
  - Turn the other side on to switch to it. The first side's time is kept, and its switch turns
    off.
  - Turn the running side off to stop the whole session and log it: one nursing record with each
    side's whole minutes (rounded half up), the side it ended on, and the start time. A stop within
    30 seconds is saved as 1 minute on that side, and more than 240 minutes on a side is saved as
    240.
  - Turning on the side that's already running, or off a side that isn't, does nothing.
  - **The switches show only Home Assistant's own timer.** The app doesn't sync a running nursing
    timer, so Home Assistant can't see one running on a phone, and the phones see Home
    Assistant's only when it stops. They do what the `start_nursing` (which also switches
    sides) and `stop_nursing` actions do.
  - The timer survives a restart (if it can't be saved, the change still happens and an error says
    so), and nursing while the baby is asleep is fine. A timer for a baby no longer in the account
    is dropped.
- **Nursing left time** and **Nursing right time** show the minutes on each side this session,
  including the running side's time so far, to a tenth of a minute. They refresh every 30 seconds
  while a session runs, with no sync needed, and are unknown (like the timer sensors) when no
  session runs.
- **Discard nursing** drops the running nursing timer without writing anything (the
  `discard_nursing` action). With no timer running it shows an error.
- **Pump timer**, on the account device, runs Home Assistant's own pump timer. A pump belongs to
  the account, not a baby. Turn it on to start, and off to stop and log the session: one pump
  record starting when the timer did, with the same whole minutes (rounded half up, at least 1, at
  most 240) on both sides, as the app writes a timed pump with both sides together, and an
  **amount of 0**, as the app does for a timed pump with no volume. In the app, the timer and the amount are one step; here, use `stop_pump` with an
  `amount` (a dashboard card or script can offer both) to save the total with the minutes. There is
  deliberately no separate amount entity.
  - Turning it on while it runs, or off when it doesn't, shows an error and writes nothing.
  - Like the nursing timer, it lives only in Home Assistant until it stops: nothing posts at the
    start, the phones don't see it running, and it survives a restart (if it can't be saved, the
    change still happens and an error says so).
  - It does what the `start_pump` and `stop_pump` actions do. The **Pump timer** sensor shows its
    start time.
- While a sleep, a nursing timer, or the pump timer runs, Home Assistant syncs every minute, so a phone's stop shows
  up quickly. It returns to every 5 minutes when none is running. A nursing or pump timer that has
  run for an hour or more no longer counts: it's probably forgotten, and a
  [repair](#troubleshooting) asks you to stop (or discard, for nursing) it. A running sleep always counts, however long, because the app syncs it
  and every phone shows it.
- A sleep can't be longer than 24 hours. Stopping one later than that saves 24 hours, so a
  forgotten timer can always be stopped; fix the length in the app.

The actions do the same and more. A baby action takes the baby's device as `device_id`. The
account actions (`log_pump`, `start_pump`, `stop_pump`, and `undo` for a whole account) take
`config_entry_id`.

**Upgrading automations:** the old `baby` field (and `device` on `log_pump`) still works for one
more release. Using it raises a repair in **Settings > System > Repairs** that names the action
and the field to switch to: `device_id` for a baby, and `config_entry_id` for an account.


- `nighp_baby_tracker.log_diaper` takes `device_id` (the baby's device), `type` (`wet`, `dirty`, or
  `mixed`), an optional `time` (default now; a time with no offset is read in Home Assistant's time
  zone), and an optional `note`.
- `nighp_baby_tracker.log_bottle` takes `device_id`, `kind` (`formula` or `pumped`), `amount` (more than
  zero), an optional `unit` (`fl. oz.` or `mL`), an optional `time`, and an optional `note`.
- `nighp_baby_tracker.log_nursing` takes `device_id`, `left_minutes` and `right_minutes` (whole
  minutes; at least one above zero), an optional `finish_side` (`left` or `right`; default the last
  side used, right when both), an optional `end_time` (default now), and an optional `note`. The
  record starts at the end time minus the total minutes.
- `nighp_baby_tracker.log_sleep` takes `device_id`, `start` and `end` (the end must be after the start),
  and an optional `note`. The length is saved in whole minutes, rounded half up, and never less
  than 1.
- `nighp_baby_tracker.log_pump` takes `config_entry_id` (the account), an optional `unit` (`fl. oz.`
  or `mL`), an optional `finish_side`, an optional `time` (the start; default now), an optional
  `note`, and the volume and length, either as a total or by side:
  - **A total**, which is how most pumps are logged in the app: `amount` (the total volume) and,
    if you like, `minutes` (the session's length with both sides together, written as the same
    minutes on each side). The record has the total and no per-side amounts. `minutes` of 0 is
    allowed.
  - **By side**: `left_amount` and/or `right_amount`, and `left_minutes` and `right_minutes`. The
    total is the sum of the sides.

  Passing `amount` with `left_amount` or `right_amount`, or `minutes` with `left_minutes` or
  `right_minutes` (even 0), is an error and posts nothing. A pump belongs to the account, so no baby
  is saved with it.
- `nighp_baby_tracker.start_pump` takes `config_entry_id` and starts Home Assistant's pump timer.
  It's refused while the timer runs.
- `nighp_baby_tracker.stop_pump` takes `config_entry_id`, an optional `amount` (the total, more
  than zero), an optional `unit`, and an optional `note`. It saves one pump with the elapsed
  whole minutes on both sides, and the amount if you gave one. It's refused when no timer runs, and
  a bad amount is refused with the timer still running.
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
- `nighp_baby_tracker.discard_nursing` takes `device_id` and throws away Home Assistant's running
  nursing timer without saving anything. Use it for a timer started or left running by mistake. It
  is refused when that baby has no timer running, and it never contacts the service.
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
  timer, so Home Assistant never sees it. Only timers started from Home Assistant (the **Nursing
  left** and **Nursing right** switches or actions) do. Sleep timers show from any device.
- If Home Assistant restarts mid-session, a running timer's activity stays as it was and the
  blueprint sends nothing new until the timer changes; ending it still ends the activity.
- A sync failure mid-timer (running, then unavailable, then unknown) still ends the activity; going unavailable and back to the same running timer leaves it alone.
- The blueprint runs queued, so a start, a switch, and an end are sent in order.

## Use cases

The examples use a baby named "Baby", so the entity IDs read `sensor.baby_last_diaper` and so on.
Yours follow your baby's name.

**A crib-side button.** Put the **Wet diaper**, **Dirty diaper**, and **Bottle** buttons on a
dashboard, or map a physical button to `button.press` on one of them. One press logs the record
now, and the phones show it after their next sync. A mistaken press is fixed with
`nighp_baby_tracker.undo`.

**A 3 a.m. dashboard.** One card with **Last diaper**, **Last feed**, and **Asleep**, plus the
buttons. The last-activity sensors are timestamps, so the card can show "23 minutes ago" instead of
a clock time. The **Sleep** switch shows whether a sleep is running and starts or ends it, so a
night wake-up needs one tap. For feeds, put **Nursing left**, **Nursing right**, and the two time
sensors on the card: tap a side to start, tap the other to switch, and tap the lit side to stop and
log the session. The time sensors show how long each side has gone.

**A pumping card.** On the account device, put the **Pump timer** switch next to **Last pump**. Tap
the switch to start a session and again to stop it. To save the volume with the minutes, add a
button (or a script) that calls `stop_pump` with an amount, for example by asking for it with an
`input_number` helper:

```yaml
script:
  stop_pump_with_amount:
    alias: Stop pump and save the amount
    sequence:
      - action: nighp_baby_tracker.stop_pump
        data:
          config_entry_id: YOUR_CONFIG_ENTRY_ID
          amount: "{{ states('input_number.pumped_amount') | float }}"
          unit: fl. oz.
```

Pick the account from the action's form in **Developer tools > Actions** to get the right
`config_entry_id`. A pump logged afterwards with only a total, with no timer, uses `log_pump` with
`amount` and `minutes`.

**Quiet notifications while the baby sleeps.** Mute announcements, or dim a hallway light, while
`binary_sensor.baby_asleep` is on. It turns on for a sleep started from any phone as well as from
Home Assistant, after the next sync.

```yaml
automation:
  - alias: Quiet the house while Baby sleeps
    triggers:
      - trigger: state
        entity_id: binary_sensor.baby_asleep
        to: "on"
    actions:
      - action: input_boolean.turn_on
        target:
          entity_id: input_boolean.quiet_mode
  - alias: Back to normal when Baby wakes
    triggers:
      - trigger: state
        entity_id: binary_sensor.baby_asleep
        to: "off"
    actions:
      - action: input_boolean.turn_off
        target:
          entity_id: input_boolean.quiet_mode
```

Make `input_boolean.quiet_mode` a helper (**Settings > Devices & services > Helpers**), then check
it in the conditions of the automations that make noise.

**A live activity on the lock screen.** The blueprint described in
[Live activity on your iPhone](#live-activity-on-your-iphone) shows a running sleep or nursing
timer as an iOS Live Activity.

**A daily intake check.** **Diapers today**, **Feeds today**, and **Bottle volume today** count
from local midnight. This sends a message at 8 p.m. if there have been fewer than four feeds:

```yaml
automation:
  - alias: Few feeds today
    triggers:
      - trigger: time
        at: "20:00:00"
    conditions:
      - condition: numeric_state
        entity_id: sensor.baby_feeds_today
        below: 4
    actions:
      - action: notify.mobile_app_your_phone
        data:
          message: Baby has had fewer than four feeds today.
```

Change the number to suit your baby, and use your own phone's `notify` action, as described under
[Live activity on your iPhone](#live-activity-on-your-iphone).

## Known limitations

- **A running nursing timer on a phone isn't visible.** The app doesn't sync one until it stops.
  Home Assistant shows only its own nursing timer (the **Nursing left** and **Nursing right**
  switches). A sleep timer is synced, so it shows from any device.
- **A forgotten nursing or pump timer is only caught after an hour.** Because only Home Assistant
  can see these timers, one left running keeps going until you stop it (or discard a nursing one).
  After an hour a repair appears and Home Assistant goes back to checking every 5 minutes. A running
  sleep has no such limit. A forgotten pump timer is stopped with the **Pump timer** switch or
  `stop_pump`, which saves up to 240 minutes; `undo` then deletes that record if it wasn't real.
- **A pump timer's stop has no amount by default.** The switch can't ask for one, so use
  `stop_pump` with an `amount` when you want it saved with the minutes. Without one the record's
  amount is 0, as the app writes it, and **Last pump** shows no amount for it.
- **Records arrive on a schedule, not instantly.** Home Assistant checks for new records every 5
  minutes, and every minute while a sleep timer, or a nursing or pump timer under an hour old, runs. No option
  changes this. Records
  Home Assistant writes show at once. Call `homeassistant.update_entity` to check sooner.
- **A sleep is at most 24 hours.** Stopping a sleep later than that saves 24 hours. Fix the length
  in the app. A sleep can't start in the future or more than 24 hours ago, and nursing and pump
  minutes are capped at 240 per side.
- **The service has no public API.** The integration speaks the app's own sync protocol, which
  `PROTOCOL.md` documents from observed traffic. Nighp can change it without notice, and a change
  may stop the integration until it's updated.
- **The iPhone live activity needs the Companion app,** iOS 17.2 or later, and Home Assistant
  2026.7.0 or later. See [Live activity on your iPhone](#live-activity-on-your-iphone).
- **Undo only removes Home Assistant's own entries.** `nighp_baby_tracker.undo` can't delete a
  record a phone made. Delete those in the app.
- **Pumps belong to the account,** not a baby, so **Last pump** is on the account device.
- **Home Assistant is its own device** in the account, named "Home Assistant". It never uses a
  phone's identity, so it doesn't appear in the app's device list until it writes its first
  record.

## Troubleshooting

**The integration asks you to sign in again.** The service rejected the saved password, usually
because it changed. Follow the prompt in **Settings > Devices & services** and enter the current
password. See [If your password changes](#if-your-password-changes).

**A repair says Baby Tracker can't write for your account.** Home Assistant's own log in the
account has entries it can't read back, and every write has failed for over an hour. Writing is
paused so nothing is skipped or overwritten. To fix it:

1. Go to **Settings > Devices & services** and open **Baby Tracker (Nighp)**.
2. Open the menu next to your account, choose **Delete**, and confirm.
3. Add **Baby Tracker (Nighp)** again with the same email and password.

Adding it again reloads the account's history, and Home Assistant appears as a new device in the
app. The old "Home Assistant" device stays in the app's device list. A shorter gap shows only an
error on the write.

**A repair says the pump timer has run for over an hour.** Home Assistant's pump timer is probably
forgotten. Turn off the **Pump timer** switch, or call `stop_pump`, to save the session (at most
240 minutes), then use `nighp_baby_tracker.undo` for the account if the record wasn't real. The
repair goes away when the timer stops.

**A repair says an automation uses an old field.** The `baby` field (and `device` on `log_pump`)
still works for one more release. The repair names the action and the field to switch to:
`device_id` for a baby, and `config_entry_id` for an account. Edit the automation, script, or
dashboard button that calls the action, save it, and run it again. The repair clears on its own
after the next restart unless something still uses the old field.

**A repair says a nursing timer has run for over an hour.** Home Assistant's own nursing timer for
that baby was probably forgotten: the app doesn't show a running nursing timer, so nothing else
will end it. Either stop it (turn off the **Nursing** switch that is on, or call
`nighp_baby_tracker.stop_nursing`) to save one nursing record, with each side capped at 240
minutes, or press **Discard nursing** (or call `nighp_baby_tracker.discard_nursing`) to throw it
away without saving anything. The
repair clears when the timer stops or is discarded, and returns after a restart if the timer is
still over an hour old. This guard applies only to nursing: a running sleep is synced, so it is
never flagged or capped.

**A write says "the last entry is still being confirmed."** The service didn't answer the last
write. Wait a minute and try again. Reusing the same `object_id` on a retry adds the record once.

**A sensor looks stale.** Phone records arrive at the next sync, within 5 minutes (1 minute while a
timer runs). To check now, call `homeassistant.update_entity` on any Baby Tracker entity. If the
sensors say unavailable, the sync is failing: look in the Home Assistant log for the error. A baby
deleted in the app disappears at the next sync, as described under
[Removing a baby](#removing-a-baby).

**A press or action shows an error.** The message says why. Common causes are a sleep already
running or already ended on another device, no nursing timer running, and an amount or length out
of range.

**Download diagnostics** for a bug report: on the integration's page, choose **Download
diagnostics**. See [Diagnostics](#diagnostics) for what it holds and redacts. Report problems at
<https://github.com/wtadler/ha-baby-tracker/issues>.

## Removing a baby

A baby you delete in the app is removed from Home Assistant, with its device and entities, at the
next sync. A baby you add back later gets its device and entities again. If a baby's device is
ever left behind, you can delete it from its device page (**Delete** in the device's menu).
A baby still on the account can't be deleted, because the next sync would add it back, and neither
can the account's own device.

## Diagnostics

On the integration's page, choose **Download diagnostics** to attach to a bug report. It holds the
settings, the sync state, and counts of records. The email, password, and device ID are redacted,
and it holds no baby's name or ID and no notes.

## Removal

1. Go to **Settings > Devices & services**, open **Baby Tracker (Nighp)**, and choose **Delete**.
   This also deletes the saved sync state.
2. Delete the `custom_components/nighp_baby_tracker` folder and restart Home Assistant.

## Core submission

This repository is a custom integration, so its manifest keeps a few keys that core handles
differently. At core submission:

- `documentation` points at the page on home-assistant.io.
- `issue_tracker` and `version` are removed: core leaves them out.
- `categories` is added as `["health_wellbeing"]`. The hassfest image CI runs doesn't accept the key
  yet, so it waits.
- `loggers` becomes `["pybabytracker"]`, the library's own logger, once the library is a PyPI
  package (#12). The bundled copy logs under its path inside this integration, so the entry today
  is redundant: Home Assistant's logger integration already covers the integration's own package.
  It is kept to record the intent.
- `requirements` pins that package, which is also what lets `dependency-transparency` be marked
  done.
- `quality_scale` is added with the highest tier that holds. It can't be declared today: Bronze
  needs `dependency-transparency`.

`quality_scale.yaml` lists all 54 rules. A test pins the rule list to Home Assistant's, and
hassfest checks the file's shape for core integrations only.

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
