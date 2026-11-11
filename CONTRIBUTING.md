# Contributing

## What belongs here

This integration is headed for Home Assistant core, so every entity and action in it will be part
of a core pull request and has to justify itself to core reviewers. The library,
[`pybabytracker`](https://github.com/wtadler/pybabytracker), is where the app's full data model
lives: it reads and writes every record type the app syncs, whether or not the integration uses it.
A record type the library supports isn't a reason to add it here.

A feature belongs in the integration if it gives Home Assistant something to do that the app
can't:

- **Automations on current state:** "fed 3 hours ago," the next medicine dose, a running sleep
  timer.
- **Quick logging away from a phone:** buttons, NFC tags, and voice, for frequent, time-sensitive
  records such as diapers, bottles, sleep, and nursing.
- **Numbers worth a graph:** daily totals, temperature, and growth.

It doesn't belong if it only repeats what the app already shows, such as a date for a rare event,
or setup done once in the app.

Three more rules:

- **Generic, not named.** No feature depends on a name the user typed, like an activity called
  "Bath." Types are handled the same way and keyed by ID.
- **Enabled by default, or not at all.** If an entity is only worth adding switched off, it
  probably shouldn't be added.
- **No HACS-only features.** Once the core pull requests land, this repository and core should
  match. Code marked `# HACS-only` is only for migrations and deprecations, which core never needs.

The decision, and the discussion behind it, is in
[#161](https://github.com/wtadler/ha-baby-tracker/issues/161). Bug reports and fixes are always
welcome. For a new feature, open an issue first, so we can agree it fits before you write it.

## Development

```sh
uv venv --python 3.14 .venv
uv pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check .
```

The library that talks to the service is [`pybabytracker`](https://github.com/wtadler/pybabytracker),
published on PyPI and pinned in `manifest.json` and `requirements_test.txt`.

`PROTOCOL.md` documents the sync service, and `GLOSSARY.md` the terms used in the code.

## Core submission

This repository is a custom integration, so its manifest keeps a few keys that core handles
differently. At core submission:

- `documentation` points at the page on home-assistant.io.
- `issue_tracker` and `version` are removed: core leaves them out.
- `requirements` keeps the `pybabytracker` pin, and `loggers` keeps `["pybabytracker"]`.
- `quality_scale` is added with the highest tier that holds, which is Gold today (`strict-typing`,
  a Platinum rule, is still open). A custom integration's manifest leaves the key out.

`quality_scale.yaml` lists all 54 rules. A test pins the rule list to Home Assistant's, and
hassfest checks the file's shape for core integrations only. `core-prep/README.md` describes how the
integration is cut into core pull requests.
