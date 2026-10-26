"""Config flow for Baby Tracker (Nighp)."""

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow

from .const import DOMAIN
from .pybabytracker import BabyTrackerClient  # noqa: F401

_SCHEMA = vol.Schema({vol.Required("email"): str, vol.Required("password"): str})


class BabyTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is None:
            return self.async_show_form(
                step_id="init", data_schema=vol.Schema({vol.Required("scan_interval"): int})
            )
        return self.async_abort(reason="not_implemented")


class BabyTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return BabyTrackerOptionsFlow()

    async def async_step_user(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_SCHEMA)
        return self.async_abort(reason="not_implemented")

    async def async_step_reauth(self, entry_data):
        return self.async_show_form(step_id="reauth_confirm", data_schema=_SCHEMA)

    async def async_step_reauth_confirm(self, user_input=None):
        return self.async_abort(reason="not_implemented")
