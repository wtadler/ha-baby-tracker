# Contributing

## Development

```sh
uv venv --python 3.14 .venv
uv pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check .
```

The library is vendored while it's private. To update the bundled copy, run
`scripts/vendor_library.sh <path-to-pybabytracker>`. It copies the package and writes the source
commit to `custom_components/baby_tracker/pybabytracker/VENDORED_FROM`. The vendored copy is
excluded from this repository's lint rules, because it's checked in the library's own repository.

`PROTOCOL.md` documents the sync service, and `GLOSSARY.md` the terms used in the code.

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
hassfest checks the file's shape for core integrations only. `core-prep/README.md` describes how the
integration is cut into core pull requests.
