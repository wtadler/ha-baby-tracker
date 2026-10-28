"""The bottle units and kinds, and the bottle the options describe."""

import math
from collections.abc import Mapping
from typing import Any

from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant

from .const import CONF_BOTTLE_SIZE, CONF_BOTTLE_TYPE, CONF_BOTTLE_UNIT
from .pybabytracker import UNIT_MILLILITERS, UNIT_OUNCES

# Home Assistant's names for the two bottle units, and the library's.
OUNCES = UnitOfVolume.FLUID_OUNCES.value
MILLILITERS = UnitOfVolume.MILLILITERS.value
VOLUME_UNITS = {OUNCES: UNIT_OUNCES, MILLILITERS: UNIT_MILLILITERS}
BOTTLE_KINDS = ("formula", "pumped")

DEFAULT_BOTTLE_KIND = "formula"
DEFAULT_BOTTLE_OUNCES = 4
DEFAULT_BOTTLE_MILLILITERS = 120


def default_bottle(hass: HomeAssistant) -> tuple[float, str, str]:
    """Return the bottle size, unit, and type used until the options say otherwise.

    120 mL in a metric unit system, 4 fl. oz. otherwise.
    """
    if hass.config.units.volume_unit == UnitOfVolume.LITERS:
        return DEFAULT_BOTTLE_MILLILITERS, MILLILITERS, DEFAULT_BOTTLE_KIND
    return DEFAULT_BOTTLE_OUNCES, OUNCES, DEFAULT_BOTTLE_KIND


def configured_bottle(hass: HomeAssistant, options: Mapping[str, Any]) -> tuple[float, str, str]:
    """Return the bottle the options describe.

    Saved options that don't make sense (any one of the three) give the defaults.
    """
    size = options.get(CONF_BOTTLE_SIZE)
    unit = options.get(CONF_BOTTLE_UNIT)
    kind = options.get(CONF_BOTTLE_TYPE)
    if (
        isinstance(size, int | float)
        and not isinstance(size, bool)
        and math.isfinite(size)
        and size > 0
        and unit in VOLUME_UNITS
        and kind in BOTTLE_KINDS
    ):
        return size, unit, kind
    return default_bottle(hass)
