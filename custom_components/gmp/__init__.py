"""The Green Mountain Power integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY_ID,
    CONF_API_KEY_SECRET,
    CONF_REBUILD_SITE,
)
from .coordinator import GmpConfigEntry, GmpCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry up to the current version, one step at a time.

    Version 1 held a portal password. Nothing converts it -- a password cannot
    become an API key -- so the credentials are dropped and setup asks for a
    key through reauth. Versions 2 and 3 carry site-consumption statistics
    that cannot be corrected in place -- computed from a field that misreports
    while the generation channel is silent, then left half rebuilt -- so they
    are cleared for the next refresh to rebuild.
    """
    if entry.version == 1:
        account = entry.data[CONF_ACCOUNT_NUMBER]
        hass.config_entries.async_update_entry(
            entry, data={CONF_ACCOUNT_NUMBER: account}, version=2
        )
        _LOGGER.info("GMP entry migrated to API key auth; a new key is needed")
    if entry.version in (2, 3):
        # Site consumption cannot be corrected in place: version 2 wrote it
        # straight from GMP's totalEnergyUsed, which counts an unreported
        # generation figure as zero and so goes negative under the array, and
        # version 3 could be left half rebuilt by a restart. Clearing it is
        # the coordinator's job rather than this one -- it has to happen in
        # the same breath as resetting the cursor that decides what to fetch,
        # or the refresh reads the rows on their way out and appends to them.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REBUILD_SITE: True}, version=4
        )
        _LOGGER.info("Site consumption will be rebuilt on the next refresh")
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
