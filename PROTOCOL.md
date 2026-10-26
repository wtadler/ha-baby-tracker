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

## OPCodes

- `0`: add (observed)
- `2`: delete (observed; the payload is the full record)
- `1`: probably update (not yet observed)

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
- `time` is when the event happened and `timestamp` is when the record was last changed. Both are UTC strings. `timezone` is the offset in seconds.
- Diaper `status`: 0 = wet, 1 = dirty, 2 = mixed (from the local data; dry not yet confirmed).
- The sending device sets `newFlage` to `"true"` on an add and `"false"` on a delete.
- The baby record is embedded in every entry.

## Local database (`.btcp` clone, `.btbk` backup)

- Both are zips containing `EasyLog.db` (SQLite) plus photos.
- `Time` columns are Unix epoch seconds.
- Tables: `Baby`, `Diaper`, `Nursing`, `Formula`, `Pumped`, `Pump`, `Sleep`, `Growth`, `Medicine`, `Temperature`, `Vaccine`, `Milestone`, `OtherActivity`, and more.
- `*Desc` and `*Selection` tables hold lookup lists, such as feed types and sleep locations.

## Open questions

- When Home Assistant's first post makes it appear in the device list, is there a confirmation step or a device limit? Confirm on the test account in the first write ticket.
- In the capture, the pull from another device returned a delete matching ours (SyncID 1602). Does the server echo deletes, or did that device send its own copy?
- Payloads for `Nursing`, `Formula`, `Sleep` (start, stop, in-progress), and `Pump` still need to be captured. Their tables suggest the fields.
