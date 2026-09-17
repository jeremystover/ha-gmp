"""The Green Mountain Power integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import CONF_ACCOUNT_NUMBER, CONF_API_KEY_ID, CONF_API_KEY_SECRET
from .coordinator import GmpConfigEntry, GmpCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move a version 1 entry off the portal password and onto an API key.

    There is nothing to convert -- a password cannot become a key -- so the old
    credentials are dropped and setup asks for the key through reauth.
    """
    if entry.version == 1:
        account = entry.data[CONF_ACCOUNT_NUMBER]
        hass.config_entries.async_update_entry(
            entry, data={CONF_ACCOUNT_NUMBER: account}, version=2
        )
        _LOGGER.info("GMP entry migrated to API key auth; a new key is needed")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GmpConfigEntry) -> bool:
    """Set up one GMP account from a config entry."""
    if not entry.data.get(CONF_API_KEY_ID) or not entry.data.get(CONF_API_KEY_SECRET):
        raise ConfigEntryAuthFailed("GMP API key needed")
    coordinator = GmpCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GmpConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
