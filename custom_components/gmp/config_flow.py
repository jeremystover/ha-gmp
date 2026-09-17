"""Config flow for the Green Mountain Power integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Account, GmpAuthError, GmpClient, GmpError
from .const import CONF_ACCOUNT_NUMBER, CONF_API_KEY_ID, CONF_API_KEY_SECRET, DOMAIN

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY_ID): str,
        vol.Required(CONF_API_KEY_SECRET): str,
    }
)


class GmpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Take the API key, then pick the service account to import."""

    VERSION = 3

    def __init__(self) -> None:
        self._key_id: str = ""
        self._key_secret: str = ""
        self._accounts: list[Account] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the API key and discover the accounts behind it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._key_id = user_input[CONF_API_KEY_ID].strip()
            self._key_secret = user_input[CONF_API_KEY_SECRET].strip()
            client = GmpClient(async_get_clientsession(self.hass), self._key_id, self._key_secret)
            try:
                self._accounts = await client.async_get_accounts()
            except GmpAuthError:
                errors["base"] = "invalid_auth"
            except GmpError:
                errors["base"] = "cannot_connect"
            else:
                if len(self._accounts) == 1:
                    account = self._accounts[0]
                    return await self._async_create(account.number, account.label)
                return await self.async_step_account()

        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of several accounts, or type one if none were listed."""
        if user_input is not None:
            number = str(user_input[CONF_ACCOUNT_NUMBER]).strip()
            account = next((a for a in self._accounts if a.number == number), None)
            return await self._async_create(number, account.label if account else f"GMP {number}")

        if self._accounts:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_NUMBER): vol.In(
                        {a.number: a.label for a in self._accounts}
                    )
                }
            )
        else:
            schema = vol.Schema({vol.Required(CONF_ACCOUNT_NUMBER): str})
        return self.async_show_form(step_id="account", data_schema=schema)

    async def _async_create(self, number: str, title: str) -> ConfigFlowResult:
        await self.async_set_unique_id(number)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"GMP {title}",
            data={
                CONF_API_KEY_ID: self._key_id,
                CONF_API_KEY_SECRET: self._key_secret,
                CONF_ACCOUNT_NUMBER: number,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """GMP rejected the stored key, or an older entry has none."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a new API key for the existing entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            key_id = user_input[CONF_API_KEY_ID].strip()
            key_secret = user_input[CONF_API_KEY_SECRET].strip()
            client = GmpClient(async_get_clientsession(self.hass), key_id, key_secret)
            try:
                await client.async_get_accounts()
            except GmpAuthError:
                errors["base"] = "invalid_auth"
            except GmpError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_API_KEY_ID: key_id,
                        CONF_API_KEY_SECRET: key_secret,
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=USER_SCHEMA,
            errors=errors,
            description_placeholders={"account": entry.data[CONF_ACCOUNT_NUMBER]},
        )
