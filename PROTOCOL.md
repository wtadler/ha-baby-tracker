# Baby Tracker (Nighp) sync protocol

Captured 2026-10-26 from app build 294 (iOS) with mitmproxy. No certificate pinning.

Notes about the Android app come from records seen on a real account and from how the app behaves, and most weren't captured on the wire. Notes marked **unconfirmed** haven't been seen on the wire.

## Server

- Base URL: `https://prodapp.babytrackers.com`
- Apache/PHP. Session is a cookie set on the first request and authorized by `POST /session`.
- Every body is JSON. Responses are labeled `text/html` but contain JSON.
- The app's user agent is `BabyTracker/294 CFNetwork/... Darwin/...`.

## Endpoints

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/session` | `{"EmailAddress", "Password", "Device": {"DeviceUUID", "DeviceName", "DeviceOSInfo"}, "AppInfo": {"AppType": 0, "AccountType": 0}}` | `200 {"AccountID": n}` |
| GET | `/account/device` | (none) | `[{"DeviceUUID", "LastSyncID", "DeviceName", "DeviceOSInfo"}, ...]` for every device in the sync group |
| POST | `/account/transaction` | `{"OPCode", "SyncID", "Transaction"}` | `201`, empty body |
| GET | `/account/transaction/{DeviceUUID}/{SinceSyncID}` | (none) | `[{"SyncID", "OPCode", "Transaction"}, ...]`, that device's transactions after `SinceSyncID` |
| POST | `/account/plus` | `{"Subscriptions": []}` | `{"Entitled": false, ...}`. Subscription check; sync works without Plus. |

Without a session, `GET /account/device` returns `401 Unauthorized`.

How the Android app handles the `POST /session` response (unconfirmed):

- `200` is success, with the `AccountID` in the body. When joining, an `AccountID` of 0 counts as a failure.
- `401` or `400` is a wrong email or password.
- `307` means the password was changed on another device.
- When any other call returns `401`, the app signs in again with the saved email and password and retries. If that sign-in returns `401` or `307`, it turns sync off.
- It sends `DeviceOSInfo` as `<model> OS <Android SDK level>` and the same `AppInfo` as iOS, with `AppType` 0 and `AccountType` 0.

## Sync model

- Each device has its own log of transactions. `SyncID` counts up within each device's log: the sender uses its own last ID + 1.
- A device sends its changes with `POST /account/transaction`. To pull, it reads `/account/device` for every device's `LastSyncID` and fetches from each one that's ahead of what it has seen. The app stores what it has seen in the local `MergedTransaction(DeviceID, SyncID)` table.
- Changes not yet sent wait in the local `TransactionLog(ID, OpCode, Log)` table, where `Log` holds the same base64 string.

### Probe findings (2026-10-26, read-only, with `python -m pybabytracker --probe-history`)

- **The server keeps full history.** Every device's log on a real account came back starting from `SyncID` 1, including logs going back about two years. A new device can backfill by replaying every log from 0. No snapshot import is needed.
- **Logs come in pages of 500.** `GET /account/transaction/{DeviceUUID}/{SinceSyncID}` returns at most 500 transactions. Logs longer than that need repeated reads from the highest number returned, until a read comes back short or reaches `LastSyncID`.
- **Signing in doesn't join the sync group.** A `POST /session` with a new `DeviceUUID` succeeds, but that device doesn't appear in `/account/device`. On a fresh test account, the iPad appeared (with `LastSyncID` 1) only after it posted its first transaction. A device seems to join the group by posting, which suggests a read-only client stays invisible in the device list.
- A full probe of 5 devices (about 4,300 transactions) took about 4 seconds.
- **A device joins by posting, with no confirmation step** (2026-10-26, test account, `--write-test-diaper`). The new dev device was absent from `/account/device` until its first `POST /account/transaction` (SyncID 1, a diaper add), then listed at once with `LastSyncID` 1, then 2 after a delete. Reading an unlisted device's log returns `[]`. The posted transaction came back byte-identical in the device's own log.

### How the Android app pulls and merges

Unconfirmed, apart from the delete echo noted below:

- **Progress per device.** The app tracks the highest `SyncID` it has merged from each other device. It skips its own device in `/account/device` and reads a device it has never seen from 0.
- **Paging.** It keeps reading a device's log while a read returns 500 or more items, each time from the last `SyncID` returned. That matches the probe findings above.
- **Add and update.** For records (not babies or selections), an add for a record the app already has is applied as an update, and an update for a record it doesn't have is applied as an add. An update replaces the stored record only when the stored `timestamp` is older, so the newest `timestamp` wins.
- **Deletes are repeated.** After the app merges a delete from another device, it posts the same delete under its own device ID. That fits the delete in the capture below that matched ours.
- **Joining.** A joining device gets a new device ID each time. It signs in, downloads the group's seed database (`GET /account/seed`), replaces its own database with it, and downloads the pictures. It uploads its old data to `/account/clonedata` first if it couldn't back it up to a cloud service.
- **No echoes while joining.** Until the first full pull after a join finishes, merges post nothing back: no repeated deletes, relives, or conflict records.

## OPCodes

- `0`: add.
- `1`: update. The payload is the full record with a newer `timestamp` and `newFlage` set to `"false"`.
- `2`: delete. The payload is the full record as last saved, and its `timestamp` is **not** bumped.

All three were observed in captures on 2026-10-26.

Two more are used for babies and [selections](#selections) only. Neither was captured (unconfirmed):

- `3`: relive. A device that refuses a delete because the baby or selection still has records posts this, carrying its own copy, so the other devices restore it.
- `4`: conflict. The payload has a `winner` and a `loser`, each a full baby or selection. Receivers move the loser's records to the winner. See [Selections](#selections).

## Timers

Captured on the test account on 2026-10-26, with the sync screen opened while each timer was running.

- **Sleep timers sync while running.** Starting a sleep posts an **add** right away with `duration` 0 and `time` set to the start. Stopping it posts an **update** to the same `objectID` with the final `duration`. Each later edit, such as adding a note, posts another update. A sleep with `duration` 0 is therefore a sleep in progress.
- **Nursing timers don't sync while running.** Nothing was posted when the timer started, when it switched from left to right, or while the sync screen was open. When the timer stopped, one **add** arrived with `leftDuration`, `rightDuration`, `finishSide` (0 = not set, 1 = left, 2 = right; the app labels the field as the side the session started on, see issue #123), `isDetailed` "true", and `state` 5. That fits the absence of a "last synced" update during nursing. HA can't see a nursing timer running on a phone. It can only keep its own timer and write the finished entry at the stop.
- **Pump timers (assumed, not captured).** The app's pump timer wasn't captured. Real timed pumps carry equal `leftDuration` and `rightDuration` with `sides` 0, so Home Assistant keeps its own pump timer like the nursing one and writes the finished pump at the stop. A stop with no amount writes the durations and an `amount` of 0 (checked against the app's own database: every timed pump it saved without a volume has amount 0, never a missing value).
- Durations are whole minutes. About 30 seconds per side was stored as 1.

The app maker says the following about timers, beyond what we captured:

- Running nursing, pumping, and other-activity timers are not sent to the server. Only the finished record syncs.
- Each device in a sync group needs its own unique device ID.

The maker's list doesn't name sleep. Our capture above shows sleep timers do sync while running, so we keep treating a sleep with `duration` 0 as one in progress. Because only sleeps sync mid-timer, a sleep ended on a phone can take up to one sync interval (5 minutes) to reach Home Assistant.

## Payloads by type

Fields observed beyond the shared ones (`BCObjectType`, `objectID`, `time`, `timestamp`, `timezone`, `note`, `newFlage`, `pictureLoaded`, `pictureNote`, `baby`):

| Type | Fields |
|---|---|
| `Formula` | `amount` |
| `Pumped` (a bottle of pumped milk) | `amount` |
| `Pump` | `amount` (total), `leftAmount`, `rightAmount`, `leftDuration`, `rightDuration`, `finishSide`, `sides` (a timed pump with no volume carries `amount` 0; no `baby`: a pump belongs to the account, and `leftAmount` and `rightAmount` appear only when per-side amounts were entered) |
| `Nursing` | `leftDuration`, `rightDuration`, `bothDuration`, `finishSide`, `isDetailed`, `pairSessionState`, `state` |
| `NursingSession` (Android's timed nursing) | The `Nursing` keys, with `isDetailed` spelled `detailed`, plus `leftTimestamps` and `rightTimestamps`. It counts as a nursing feed. |
| `Sleep` | `duration` (minutes; 0 while running) |
| `Diaper` | `status` (0 wet, 1 dirty, 2 mixed), `amount`, `peeColor`, `pooColor`, `texture`, `flag` |

**Android's encoding.** Android sends numeric codes (`finishSide`, `status`, `state`) as digit strings such as `"1"`, where iOS sends ints, and sends `englishMeasure` as a boolean, where the iOS captures above show a string. Android's timed nursing syncs as `NursingSession`, not `Nursing`.

**Metric volumes (observed 2026-10-26).** With the app set to metric, a 120 mL formula bottle was sent as `"amount": {"BCObjectType": "VolumeMeasure", "englishMeasure": "false", "value": 120}`. That's the number as entered, with no conversion, and `englishMeasure` is the string `"false"`; ounces use `"true"`. Pumps use the same object for `amount`, `leftAmount`, and `rightAmount`, though a metric pump wasn't captured separately. The app's unit is a global preference, not per entry.

**Volumes aren't plain numbers.** Each amount is an object: `{"BCObjectType": "VolumeMeasure", "englishMeasure": "true", "value": 4}`, where `englishMeasure` "true" means ounces. The local database's flat `Amount` and `IsEnglishScale` columns don't match the sync format.

Anonymized fixtures for all nine captured transactions are in the library's `tests/fixtures/captured_payloads.json`.

### More types, from the Android app

None of these were captured, so the keys are unconfirmed. Each record type below except `Pump` also has the shared keys, including `baby`. `Baby` and `Picture` are objects embedded in records.

| Type | Keys beyond the shared ones |
|---|---|
| `Medication` (a dose; stored in the `Medicine` table) | `amount` (a plain number), `medicationSelection` (the medicine) |
| `NursingSession` (Android's nursing timer) | see [Payloads by type](#payloads-by-type) |
| `Temperature` | `temperature` (a measure) |
| `Growth` | `weight` (a measure), `length` and `head` (each a measure), `deleted`, `queryIndex` |
| `OtherFeed` (solids and other foods) | `feedType` (the food, a selection), `desc` (a selection), `amount` (a measure), `unit` (free text) |
| `OtherActivity` | `desc` (the activity, a selection), `duration` |
| `Milestone` | `milestoneType` (a selection), `hasNewType` |
| `Vaccine` | `vaccineType` (a selection) |
| `Bath`, `Joy`, `Journal` | none |
| `Pump` (one key more than the captured table) | `label` |
| `Baby` (embedded in records) | `name`, `dob`, `dueDay`, `gender` (true = boy, false = girl), `pictureName` |
| `Picture` (each entry of `pictureNote`) | `activityID`, `attached`, `deleted`, `fileName`, `mediaType` |

- These types don't follow the column-name rule under [Transaction payload](#transaction-payload). A record names the selection it uses by embedding the whole selection, not its ID, and a measurement is a measure object, not a number and a scale flag. For example, a `Medication` carries `medicationSelection` where the `Medicine` table has `MedID`, and a `Temperature` carries `temperature` where the table has `Temp` and `IsEnglishScale`. [ha-baby-tracker#124](https://github.com/wtadler/ha-baby-tracker/issues/124) lists the gaps this left in the library.
- A `Pump` with `label` "InventoryAdjustment" isn't a pumping session but a change to the milk stash. Its `amount` is the new stash minus the old one, so it can be negative.
- `NursingSession`'s `leftTimestamps` and `rightTimestamps` are lists of `{"startItem", "time"}`, where `startItem` is a boolean. They appear to mark when each side started and stopped. That fits Android timer sessions arriving as `NursingSession` ([ha-baby-tracker#121](https://github.com/wtadler/ha-baby-tracker/issues/121)).
- The app decodes only the types in this document and the selection types under [Selections](#selections). A transaction of any other type, such as `Medicine`, fails to decode there (unconfirmed).
- A picture's file on the server is `fileName` plus `.jpg`, or `.mp4` for a video. Its thumbnail is `fileName` plus `__thumbnail.jpg`. Both were downloaded for a photo an Android phone attached to a milestone (seen 2026-11-07). The "thumbnail" was the larger file there, so don't assume it's small.

### Measures

Every measure has two keys, `englishMeasure` (a boolean) and `value` (a 32-bit float). `englishMeasure` true means the US unit (unconfirmed for Android):

| Measure | `englishMeasure` true | false |
|---|---|---|
| Volume | oz | mL |
| Weight | lb (decimal) | kg |
| Length | inches | cm |
| Temperature | °F | °C |

An `OtherFeed`'s `amount` counts its `unit`, which is free text, so its `englishMeasure` says nothing about that unit.

### Enum values

The codes in these payloads. Only the nursing and pump `finishSide` codes and the diaper `status` codes 0 to 2 were seen on the wire. The rest are unconfirmed.

| Key | Values |
|---|---|
| Diaper `status` | 0 wet, 1 dirty, 2 mixed, 3 dry |
| Diaper `amount` | 0 none, 1 light, 2 normal, 3 heavy, 4 overflow, 5 not set |
| Nursing and pump `finishSide` | 0 none, 1 left, 2 right |
| Pump `sides` | 0 both, 1 left, 2 right |
| `state`, `pairSessionState` | 0 start, 1 left running, 2 right running, 3 left paused, 4 right paused, 5 both stopped, 6 end |
| Picture `mediaType` | 0 picture, 1 video, 2 audio |

- **The app shows `finishSide` as the start side.** A nursing record with 1 reads "start side L", and one with 2 "start side R". Pumps use the same numbers.
- **Pump `sides` 0 means both.** That's why every capture has `sides` 0.
- **Dry diapers.** The app reads a wet diaper (`status` 0) with `amount` 0 as dry. Whether a phone sends a dry diaper that way or as `status` 3 wasn't checked, so read both as dry.
- The iOS capture's nursing `state` 5 is "both stopped" in this numbering.

### Android and iOS differences

The iOS side is from the captures above. The Android side is unconfirmed, except where noted.

- **Booleans.** Android sends JSON booleans for `newFlage`, `pictureLoaded`, `gender`, `englishMeasure`, and selection flags such as `prescription` and `bottle`. In the iOS captures, `newFlage`, `pictureLoaded`, `gender`, and `englishMeasure` are the strings `"true"` and `"false"`.
- **Nested objects.** Android labels only the top-level record with `BCObjectType`, and a conflict payload's `winner` and `loser`. The embedded baby, selections, measures, and pictures carry none. iOS labels the nested ones too, such as `"BCObjectType": "VolumeMeasure"`.
- **Object IDs.** Android's new IDs are lowercase UUIDs. iOS IDs are uppercase.
- **Fewer keys on Android.** Android's `Diaper` has only `status` and `amount`, with no `peeColor`, `pooColor`, `texture`, or `flag`. Its `Nursing` has only the three durations and `finishSide`. Android's timed nursing is a `NursingSession`, which has more keys than its `Nursing` (see [Payloads by type](#payloads-by-type)).
- **Dates** are the same on both: `yyyy-MM-dd HH:mm:ss +0000`, in UTC.
- **How Android writes enum codes.** An Android phone on a real account sent digit strings such as `"1"` where iOS sends ints ([ha-baby-tracker#122](https://github.com/wtadler/ha-baby-tracker/issues/122)). That was seen. Whether any Android build sends constant names instead is unconfirmed, so read an int or a digit string.
- **The iOS medicine flag.** iOS records on a real account spell `prescription` as `isPresciption` ([ha-baby-tracker#121](https://github.com/wtadler/ha-baby-tracker/issues/121)).

An Android `Medication`, with invented values (unconfirmed). `interval` is in minutes:

```json
{
  "BCObjectType": "Medication",
  "objectID": "<UUID, lowercase>",
  "time": "2026-10-25 15:40:49 +0000",
  "timestamp": "2026-10-25 15:41:02 +0000",
  "timezone": -18000,
  "amount": 2.5,
  "medicationSelection": {
    "objectID": "<medicine UUID>", "name": "Example medicine", "desc": "",
    "amountPerTime": 2.5, "unit": "ml", "interval": 360, "prescription": false,
    "newFlage": false, "timestamp": "2026-01-01 00:00:00 +0000"
  },
  "note": "", "pictureNote": [],
  "newFlage": true, "pictureLoaded": true,
  "baby": {
    "objectID": "<baby UUID>", "name": "Homer", "dob": "...", "gender": true,
    "pictureName": "...", "newFlage": false, "timestamp": "..."
  }
}
```

## Selections

Unconfirmed. A selection is an entry in one of the app's pick lists: a medicine, a food, a vaccine, a milestone, or a kind of activity. Every selection has `name` and `desc` plus the shared `objectID`, `timestamp`, and `newFlage`.

| Selection type | Extra keys | Embedded by |
|---|---|---|
| `MedicationSelection` | `amountPerTime`, `interval`, `prescription`, `unit` | `Medication.medicationSelection` |
| `OtherFeedSelection` | `bottle` | `OtherFeed.feedType` |
| `FeedDescription` | none | `OtherFeed.desc` |
| `OtherActivityDescription` | none | `OtherActivity.desc` |
| `MilestoneSelection` | `babyID`, `forAge`, `used` | `Milestone.milestoneType` |
| `VaccineSelection` | `forAge` | `Vaccine.vaccineType` |

- **Selections sync as transactions of their own**, as well as inside the records that use them. `FeedDescription` is the exception: it only travels inside an `OtherFeed`.
- **Built-in selections** have `objectID`s starting with `EasyLogSeed`.
- **Names are unique within a type.** When a new selection arrives with the name of one the device already has, the older of the two wins. If the device's own copy is older, it rejects the new one and posts a conflict (OPCode 4) naming its copy the winner. Otherwise it keeps the new one and moves its records over to it.
- **A selection in use can't be deleted.** A device that gets a delete for a selection that still has records refuses it and posts a relive (OPCode 3) of its copy. A device that gets a relive of a selection remembers it and ignores the next delete of it.
- Babies follow the same rules: unique names, conflict records, and relives.
- The app's local database has more pick lists than it syncs, such as sleep, bath, and doctor lists.

## Account ID changes

Unconfirmed. The `AccountID` from `POST /session` names the sync group. The Android app stores it when it creates, joins, or resets a group. Each time it signs in again after a `401`, it compares the new `AccountID` with the stored one. If they differ, it turns sync off and warns that sign-in failed. A different `AccountID` means the email and password now open a different group from the one the device's data came from. A client should stop syncing then and not merge into the new group.

## Other endpoints seen

- `POST /account/trial_start`, with body `{"TrialStartTime": <unix seconds>}`. The app sent this once during the capture, apparently starting a Plus trial. Home Assistant must never call it.

## Android-only endpoints

The Android app calls these beyond the ones above. None were captured, so the paths and status codes are unconfirmed, except the picture download.

| Method | Path | Use |
|---|---|---|
| DELETE | `/session` | Sign out. `200`. |
| GET | `/account/seed` | The group's seed, a zip holding its `EasyLog.db`. `200`. |
| POST | `/account/clonedata` | A joining device's old data, as multipart field `clonedata`. `201`. |
| POST | `/account/picture` | Upload a picture file, as multipart field `picture`. `201`. |
| GET | `/account/picture/{file name}` | Download a picture file. `200` with the file. For a file it doesn't have, the live service answered `400` (seen 2026-11-07 for a photo's `.mp4` name and for a name with no extension), so treat `400` and `404` both as no file. |
| POST | `/device_token` | The device's push notification token. `200` or `201`. |
| POST | `/account/forgot_password` | Ask for a password reset email. `200`. |

The Android app has no `/account/plus` or `/account/trial_start`.

**Never call `/account_reset`, `/staging_account`, `/staging_account/seed`, or `/staging_account/picture`.** They reset a group or replace it with one device's database.

## Transaction payload

`Transaction` is a base64-encoded JSON record. A diaper looks like this:

```json
{
  "BCObjectType": "Diaper",
  "objectID": "<UUID, uppercase>",
  "time": "2026-10-25 15:40:49 +0000",
  "timestamp": "2026-10-26 15:41:01 +0000",
  "timezone": -18000,
  "status": 2,
  "amount": 2, "peeColor": 5, "pooColor": 5, "texture": 5, "flag": 0,
  "note": "", "pictureNote": [],
  "newFlage": "true", "pictureLoaded": "true",
  "baby": {
    "BCObjectType": "Baby", "objectID": "<baby UUID>", "name": "...",
    "dob": "...", "gender": "true", "pictureName": "...",
    "newFlage": "false", "timestamp": "1970-01-01 00:00:00 +0000"
  }
}
```

- Field names match the columns of the matching `EasyLog.db` table, in lowerCamelCase (`Diaper.Status` becomes `status`).
  - That holds for the captured types, not for types that use a selection or a measure. See [More types, from the Android app](#more-types-from-the-android-app).
- `time` is when the event happened and `timestamp` is when the record was last changed. Both are UTC strings.
- `timezone` is the device zone's **standard** UTC offset in seconds, ignoring daylight saving, year-round (observed 2026-10-26 across about 4,300 real records). Eastern records carry -18000 in both summer and winter, and -21600 appears when the device is in Central time. To match the app, send the standard offset, not the offset in force at the time. pybabytracker's `standard_offset_seconds(tz, at)` computes it. Display isn't affected, since `time` is UTC.
  - The Android app does the same, using the device zone's offset without daylight saving (unconfirmed).
- Diaper `status`: 0 = wet, 1 = dirty, 2 = mixed (from the local data; dry not yet confirmed).
  - Android also has 3 = dry, and reads a wet diaper with `amount` 0 as dry (unconfirmed). See [Enum values](#enum-values).
- The sending device sets `newFlage` to `"true"` on an add and `"false"` on a delete.
- The baby record is embedded in every entry.
  - The baby's `gender` is true for a boy and false for a girl (unconfirmed).

## Local database (`.btcp` clone, `.btbk` backup)

- Both are zips containing `EasyLog.db` (SQLite) plus photos.
- `Time` columns are Unix epoch seconds.
- Tables: `Baby`, `Diaper`, `Nursing`, `Formula`, `Pumped`, `Pump`, `Sleep`, `Growth`, `Medicine`, `Temperature`, `Vaccine`, `Milestone`, `OtherActivity`, and more.
- `*Desc` and `*Selection` tables hold lookup lists, such as feed types and sleep locations.

## Open questions

- Is there a limit on how many devices an account can have? Joining takes no confirmation step (see Probe findings).
- In the capture, the pull from another device returned a delete matching ours (SyncID 1602). Does the server echo deletes, or did that device send its own copy?
  - If that device was an Android phone, it sent its own copy, since the Android app repeats every delete it merges (see [How the Android app pulls and merges](#how-the-android-app-pulls-and-merges)). Whether iOS does the same isn't known.
- What do the remaining types (growth, medicine, temperature, and so on) look like on the wire? They're read-only for now, and kept raw if their shape surprises the library.
  - Their Android keys are under [More types, from the Android app](#more-types-from-the-android-app). Their iOS shape is still uncaptured.
