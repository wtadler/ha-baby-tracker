"""Config flow for Baby Tracker (Nighp)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEVICE_NAME,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .pybabytracker import (
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerConnectionError,
)
from .writes import BOTTLE_KINDS, MILLILITERS, OUNCES, configured_bottle

_LOGGER = logging.getLogger(__name__)

MAX_BOTTLE_SIZE = 2000

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
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

    def __init__(self) -> None:
        # Generated once per flow, and kept in the entry: this is Home Assistant's device ID.
        self._device_uuid = str(uuid4()).upper()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BabyTrackerOptionsFlow:
        return BabyTrackerOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            # Sign in as the same device as before, so Home Assistant stays one device.
            error = await _check_login(
                self.hass, email, user_input[CONF_PASSWORD], entry.data[CONF_DEVICE_UUID]
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, user_input or {CONF_EMAIL: entry.data[CONF_EMAIL]}
            ),
            errors=errors,
        )


class BabyTrackerOptionsFlow(OptionsFlow):
    """Adjust how often Home Assistant checks for new records, and what the Bottle button logs."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_BOTTLE_SIZE: float(user_input[CONF_BOTTLE_SIZE]),
                    CONF_BOTTLE_UNIT: user_input[CONF_BOTTLE_UNIT],
                    CONF_BOTTLE_TYPE: user_input[CONF_BOTTLE_TYPE],
                }
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        size, unit, kind = configured_bottle(self.hass, self.config_entry.options)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES,
                        max=MAX_SCAN_INTERVAL_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(CONF_BOTTLE_SIZE, default=size): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1, max=MAX_BOTTLE_SIZE, step="any", mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_BOTTLE_UNIT, default=unit): SelectSelector(
                    SelectSelectorConfig(
                        options=[OUNCES, MILLILITERS], mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(CONF_BOTTLE_TYPE, default=kind): SelectSelector(
                    SelectSelectorConfig(
                        options=list(BOTTLE_KINDS),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="bottle_type",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
