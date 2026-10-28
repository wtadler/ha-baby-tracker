# Baby Tracker (Nighp) sync protocol

Captured 2026-10-26 from app build 294 (iOS) with mitmproxy. No certificate pinning.

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

## OPCodes

- `0`: add.
- `1`: update. The payload is the full record with a newer `timestamp` and `newFlage` set to `"false"`.
- `2`: delete. The payload is the full record as last saved, and its `timestamp` is **not** bumped.

All three were observed in captures on 2026-10-26.

## Timers

Captured on the test account on 2026-10-26, with the sync screen opened while each timer was running.

- **Sleep timers sync while running.** Starting a sleep posts an **add** right away with `duration` 0 and `time` set to the start. Stopping it posts an **update** to the same `objectID` with the final `duration`. Each later edit, such as adding a note, posts another update. A sleep with `duration` 0 is therefore a sleep in progress.
- **Nursing timers don't sync while running.** Nothing was posted when the timer started, when it switched from left to right, or while the sync screen was open. When the timer stopped, one **add** arrived with `leftDuration`, `rightDuration`, `finishSide` (the side last nursed, 1 = right), `isDetailed` "true", and `state` 5. That fits the absence of a "last synced" update during nursing. HA can't see a nursing timer running on a phone. It can only keep its own timer and write the finished entry at the stop.
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
| `Sleep` | `duration` (minutes; 0 while running) |
| `Diaper` | `status` (0 wet, 1 dirty, 2 mixed), `amount`, `peeColor`, `pooColor`, `texture`, `flag` |

**Metric volumes (observed 2026-10-26).** With the app set to metric, a 120 mL formula bottle was sent as `"amount": {"BCObjectType": "VolumeMeasure", "englishMeasure": "false", "value": 120}`. That's the number as entered, with no conversion, and `englishMeasure` is the string `"false"`; ounces use `"true"`. Pumps use the same object for `amount`, `leftAmount`, and `rightAmount`, though a metric pump wasn't captured separately. The app's unit is a global preference, not per entry.

**Volumes aren't plain numbers.** Each amount is an object: `{"BCObjectType": "VolumeMeasure", "englishMeasure": "true", "value": 4}`, where `englishMeasure` "true" means ounces. The local database's flat `Amount` and `IsEnglishScale` columns don't match the sync format.

Anonymized fixtures for all nine captured transactions are in the library's `tests/fixtures/captured_payloads.json`.

## Other endpoints seen

- `POST /account/trial_start`, with body `{"TrialStartTime": <unix seconds>}`. The app sent this once during the capture, apparently starting a Plus trial. Home Assistant must never call it.

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
- `time` is when the event happened and `timestamp` is when the record was last changed. Both are UTC strings.
- `timezone` is the device zone's **standard** UTC offset in seconds, ignoring daylight saving, year-round (observed 2026-10-26 across about 4,300 real records). Eastern records carry -18000 in both summer and winter, and -21600 appears when the device is in Central time. To match the app, send the standard offset, not the offset in force at the time. pybabytracker's `standard_offset_seconds(tz, at)` computes it. Display isn't affected, since `time` is UTC.
- Diaper `status`: 0 = wet, 1 = dirty, 2 = mixed (from the local data; dry not yet confirmed).
- The sending device sets `newFlage` to `"true"` on an add and `"false"` on a delete.
- The baby record is embedded in every entry.

## Local database (`.btcp` clone, `.btbk` backup)

- Both are zips containing `EasyLog.db` (SQLite) plus photos.
- `Time` columns are Unix epoch seconds.
- Tables: `Baby`, `Diaper`, `Nursing`, `Formula`, `Pumped`, `Pump`, `Sleep`, `Growth`, `Medicine`, `Temperature`, `Vaccine`, `Milestone`, `OtherActivity`, and more.
- `*Desc` and `*Selection` tables hold lookup lists, such as feed types and sleep locations.

## Open questions

- Is there a limit on how many devices an account can have? Joining takes no confirmation step (see Probe findings).
- In the capture, the pull from another device returned a delete matching ours (SyncID 1602). Does the server echo deletes, or did that device send its own copy?
- What do the remaining types (growth, medicine, temperature, and so on) look like on the wire? They're read-only for now, and kept raw if their shape surprises the library.
