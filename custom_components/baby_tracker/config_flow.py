"""Config flow for Baby Tracker (Nighp)."""

import logging
from collections.abc import Mapping  # Core PR 2
from typing import Any, override
from uuid import uuid4

import probatio
from homeassistant.config_entries import (
    ConfigEntry,  # Core PR 15
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,  # Core PR 15
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import (
    HomeAssistant,
    callback,  # Core PR 15
)
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,  # Core PR 15
    NumberSelectorConfig,  # Core PR 15
    NumberSelectorMode,  # Core PR 15
    SelectSelector,  # Core PR 15
    SelectSelectorConfig,  # Core PR 15
    SelectSelectorMode,  # Core PR 15
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from pybabytracker import (
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerConnectionError,
)

from .bottle import BOTTLE_KINDS, MILLILITERS, OUNCES, configured_bottle  # Core PR 15
from .const import (
    CONF_BOTTLE_SIZE,  # Core PR 15
    CONF_BOTTLE_TYPE,  # Core PR 15
    CONF_BOTTLE_UNIT,  # Core PR 15
    CONF_DEVICE_UUID,
    CONFIG_ENTRY_MINOR_VERSION,  # HACS-only: the config entry migration
    DEVICE_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Core PR 15
# The form's outer bound, in the larger number of the two units.
MAX_BOTTLE_SIZE = 1000
# Core PR 15
# Per unit: more than any bottle.
MAX_BOTTLE_SIZES = {OUNCES: 32, MILLILITERS: 1000}

CREDENTIALS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        probatio.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)

# Core PR 2
REAUTH_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


async def _check_login(
    hass: HomeAssistant, email: str, password: str, device_uuid: str
) -> str | None:
    """Sign in once. Returns an error key for the form, or None when the login works."""
    # Not tied to an entry, so this check detaches its own session when it's done.
    session = async_create_clientsession(hass, auto_cleanup=False)
    client = BabyTrackerClient(email, password, device_uuid, DEVICE_NAME, session=session)
    try:
        await client.sign_in()
    except AuthenticationError:
        return "invalid_auth"
    except BabyTrackerConnectionError:
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error while checking the login")
        return "unknown"
    finally:
        await client.close()
        session.detach()
    return None


class BabyTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one Baby Tracker account."""

    VERSION = 1
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION  # HACS-only: core starts at version 1.1

    def __init__(self) -> None:
        """Set up the flow with a fresh device ID."""
        # Generated once per flow, and kept in the entry: this is Home Assistant's device ID.
        self._device_uuid = str(uuid4()).upper()

    # Core PR 15
    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> BabyTrackerOptionsFlow:
        """Return the options flow."""
        return BabyTrackerOptionsFlow()

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the account's email and password, then create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()
            error = await _check_login(
                self.hass, email, user_input[CONF_PASSWORD], self._device_uuid
            )
            if error is None:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_DEVICE_UUID: self._device_uuid,
                    },
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(CREDENTIALS_SCHEMA, user_input),
            errors=errors,
        )

    # Core PR 2
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication when the saved password stops working."""
        return await self.async_step_reauth_confirm()

    # Core PR 2
    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account's password again and update the entry."""
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]
        # The account is the one the entry was set up with: stop if its unique ID names another.
        await self.async_set_unique_id(email.strip().lower())
        self._abort_if_unique_id_mismatch()
        errors: dict[str, str] = {}
        if user_input is not None:
            # Sign in as the same device as before, so Home Assistant stays one device.
            error = await _check_login(
                self.hass, email, user_input[CONF_PASSWORD], entry.data[CONF_DEVICE_UUID]
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"email": email},
            errors=errors,
        )


# Core PR 15
class BabyTrackerOptionsFlow(OptionsFlow):
    """Choose what the Bottle button logs."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and save the options."""
        errors: dict[str, str] = {}
        if user_input is None:
            size, unit, kind = configured_bottle(self.hass, self.config_entry.options)
        else:
            size = float(user_input[CONF_BOTTLE_SIZE])
            unit = user_input[CONF_BOTTLE_UNIT]
            kind = user_input[CONF_BOTTLE_TYPE]
            if size > MAX_BOTTLE_SIZES[unit]:
                errors[CONF_BOTTLE_SIZE] = "bottle_too_large"
            else:
                return self.async_create_entry(
                    data={CONF_BOTTLE_SIZE: size, CONF_BOTTLE_UNIT: unit, CONF_BOTTLE_TYPE: kind}
                )
        schema = probatio.Schema(
            {
                probatio.Required(CONF_BOTTLE_SIZE, default=size): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1, max=MAX_BOTTLE_SIZE, step="any", mode=NumberSelectorMode.BOX
                    )
                ),
                probatio.Required(CONF_BOTTLE_UNIT, default=unit): SelectSelector(
                    SelectSelectorConfig(
                        options=[OUNCES, MILLILITERS], mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                probatio.Required(CONF_BOTTLE_TYPE, default=kind): SelectSelector(
                    SelectSelectorConfig(
                        options=list(BOTTLE_KINDS),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="bottle_type",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
