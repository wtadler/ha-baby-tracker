"""Turn the library's volumes into attribute values."""

from homeassistant.const import UnitOfVolume

from .pybabytracker import Volume
from .pybabytracker.models import UNIT_MILLILITERS, UNIT_OUNCES

# The attribute's unit is an automation contract: these are Home Assistant's own constants.
_UNITS = {
    UNIT_OUNCES: UnitOfVolume.FLUID_OUNCES.value,
    UNIT_MILLILITERS: UnitOfVolume.MILLILITERS.value,
}


def volume_parts(volume: Volume | None) -> tuple[float | None, str | None]:
    """A library volume as (value, unit), with the unit named by Home Assistant's constants."""
    if volume is None:
        return None, None
    return volume.value, _UNITS.get(volume.unit)
