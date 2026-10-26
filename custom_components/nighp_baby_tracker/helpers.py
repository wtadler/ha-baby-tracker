"""Read amounts and other fields from records, whether the library typed them or kept them raw.

The library models some record types and keeps others (or ones with surprising values) as raw
payloads. Volumes in the sync format are ``VolumeMeasure`` objects, and typed models for them
are still to come in the library. Everything that reads an amount goes through ``volume_of``, so
swapping in typed volumes later means changing this file only.
"""

from __future__ import annotations

from typing import Any

UNIT_OUNCES = "oz"
UNIT_MILLILITERS = "mL"


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def record_field(record: Any, name: str) -> Any:
    """A record's field by Python name: from the typed attribute, else the raw payload."""
    value = getattr(record, name, None)
    if value is not None:
        return value
    key = _camel(name)
    for source in (getattr(record, "extra", None), getattr(record, "raw", None)):
        if isinstance(source, dict) and source.get(key) is not None:
            return source[key]
    return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _english(flag: Any) -> bool | None:
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str) and flag.lower() in ("true", "false"):
        return flag.lower() == "true"
    if isinstance(flag, int):
        return bool(flag)
    return None


def volume_of(record: Any, name: str = "amount") -> tuple[float | int | None, str | None]:
    """A volume as (value, unit). The unit is "oz", "mL", or None when the record doesn't say.

    Reads a sync-format ``VolumeMeasure`` object (as a dict or a typed object), or a plain number
    with the database's ``is_english_scale`` flag beside it. Anything else is (None, None).
    """
    raw = record_field(record, name)
    if isinstance(raw, dict):
        value = _number(raw.get("value"))
        english = _english(raw.get("englishMeasure"))
    elif isinstance(raw, int | float) and not isinstance(raw, bool):
        value = raw
        english = _english(record_field(record, "is_english_scale"))
    elif raw is not None and hasattr(raw, "value"):
        value = _number(raw.value)
        english = _english(getattr(raw, "english_measure", getattr(raw, "englishMeasure", None)))
    else:
        return None, None
    if value is None:
        return None, None
    if english is None:
        return value, None
    return value, UNIT_OUNCES if english else UNIT_MILLILITERS
