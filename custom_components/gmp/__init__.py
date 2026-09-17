"""The Green Mountain Power integration."""

from __future__ import annotations

import logging

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import CONF_ACCOUNT_NUMBER, CONF_API_KEY_ID, CONF_API_KEY_SECRET
from .coordinator import GmpConfigEntry, GmpCoordinator, statistic_id

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry up to the current version, one step at a time.

    Version 1 held a portal password. Nothing converts it -- a password cannot
    become an API key -- so the credentials are dropped and setup asks for a
    key through reauth. Version 2 carries site-consumption statistics computed
    from a field that misreports while the generation channel is silent, so
    they are cleared for the next refresh to rebuild.
    """
    if entry.version == 1:
        account = entry.data[CONF_ACCOUNT_NUMBER]
        hass.config_entries.async_update_entry(
            entry, data={CONF_ACCOUNT_NUMBER: account}, version=2
        )
        _LOGGER.info("GMP entry migrated to API key auth; a new key is needed")
    if entry.version == 2:
        # Site consumption was written straight from GMP's totalEnergyUsed,
        # which counts an unreported generation figure as zero and so goes
        # negative under the array. A stored row is never rewritten, so the
        # bad intervals have to be dropped for the next refresh to replace
        # them.
        stat = statistic_id(entry.data[CONF_ACCOUNT_NUMBER], "energy_site")
        get_instance(hass).async_clear_statistics([stat])
        hass.config_entries.async_update_entry(entry, version=3)
        _LOGGER.info("Cleared %s; it will be rebuilt on the next refresh", stat)
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
