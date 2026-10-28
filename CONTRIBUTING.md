# Contributing

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
- `categories` is added as `["health_wellbeing"]`. The hassfest image CI runs doesn't accept the key
  yet, so it waits.
- `requirements` keeps the `pybabytracker` pin, and `loggers` keeps `["pybabytracker"]`.
- `quality_scale` is added with the highest tier that holds, which is Gold today (`strict-typing`,
  a Platinum rule, is still open). A custom integration's manifest leaves the key out.

`quality_scale.yaml` lists all 54 rules. A test pins the rule list to Home Assistant's, and
hassfest checks the file's shape for core integrations only. `core-prep/README.md` describes how the
integration is cut into core pull requests.
