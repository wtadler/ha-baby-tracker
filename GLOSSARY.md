# Glossary

**Sync group**: The set of devices that share one Baby Tracker account's records. Joined by signing in with the account's email and password.

**Device**: A member of a sync group, named by a stable device ID. Each phone running the app is one device; Home Assistant joins as its own device, never borrowing a phone's ID.

**Record**: One logged item, such as a diaper change, feeding, or sleep, belonging to one baby.

**Transaction**: One change to one record (add, update, or delete), published by the device that made it. Each device's transactions form its own numbered log.

**Baby**: A child tracked in the sync group. Every record belongs to exactly one baby.

**Timer**: An activity that has started and not yet ended, such as a sleep or nursing session in progress. Ending a timer turns it into a finished record.

**Backfill**: Building Home Assistant's copy of the records for the first time by replaying every device's transaction log from the start.
