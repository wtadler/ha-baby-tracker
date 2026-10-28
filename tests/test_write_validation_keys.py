"""HACS only: every error the library's write checks raise has a translation key.

The checks live in the library (``pybabytracker.validation``) and raise typed errors;
``writes.py`` maps each type to a key in ``strings.json``. A new library error type without a key
would reach a user as a bare traceback, so this fails first. It reads ``strings.json`` from this
repository's layout, which core's tests don't have.
"""

import json
from pathlib import Path

import pytest
from pybabytracker import ValidationError

from custom_components.baby_tracker.writes import VALIDATION_KEYS

STRINGS = json.loads(
    (
        Path(__file__).parent.parent / "custom_components" / "baby_tracker" / "strings.json"
    ).read_text()
)


def _subclasses(base: type[ValidationError]) -> set[type[ValidationError]]:
    found: set[type[ValidationError]] = set()
    for child in base.__subclasses__():
        found.add(child)
        found |= _subclasses(child)
    return found


def test_every_validation_error_the_library_raises_has_a_key() -> None:
    assert _subclasses(ValidationError) == set(VALIDATION_KEYS)


@pytest.mark.parametrize("error", sorted(VALIDATION_KEYS, key=lambda kind: kind.__name__))
def test_each_key_is_a_translated_exception(error: type[ValidationError]) -> None:
    assert VALIDATION_KEYS[error] in STRINGS["exceptions"]
