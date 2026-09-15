"""Config flow for the Green Mountain Power integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Account, GmpAuthError, GmpClient, GmpError
from .const import CONF_ACCOUNT_NUMBER, DOMAIN

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class GmpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in, then pick the service account to import."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._accounts: list[Account] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect credentials and discover the accounts behind them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            client = GmpClient(async_get_clientsession(self.hass), self._username, self._password)
            try:
                await client.async_login()
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
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_ACCOUNT_NUMBER: number,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """GMP rejected the stored password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a new password for the existing entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = GmpClient(
                async_get_clientsession(self.hass),
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.async_login()
            except GmpAuthError:
                errors["base"] = "invalid_auth"
            except GmpError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )
