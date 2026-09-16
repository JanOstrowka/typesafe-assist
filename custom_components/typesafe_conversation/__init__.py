"""TypeSafe Conversation (Jev) integration for Home Assistant Assist."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    TypeSafeAuthError,
    TypeSafeClient,
    TypeSafeConnectionError,
    TypeSafeRequestError,
)
from .const import (
    CONF_BASE_URL,
    CONF_MODEL,
    CONF_TIMEOUT,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = (Platform.CONVERSATION,)

type TypeSafeConfigEntry = ConfigEntry[TypeSafeClient]


async def async_setup_entry(hass: HomeAssistant, entry: TypeSafeConfigEntry) -> bool:
    """Set up from a config entry."""
    client = TypeSafeClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        model=entry.options.get(CONF_MODEL, DEFAULT_MODEL),
        timeout=float(entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
    )
    try:
        await client.validate()
    except TypeSafeAuthError as err:
        raise ConfigEntryAuthFailed from err
    except TypeSafeConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except TypeSafeRequestError as err:
        if err.status >= 500 or err.status == 429:
            raise ConfigEntryNotReady(str(err)) from err
        # A 4xx on the validation ping means the model alias or request shape
        # is off; still load so the user can fix options without a reload loop.
        _LOGGER.warning("TypeSafe validation request rejected: %s", err)

    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TypeSafeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: TypeSafeConfigEntry
) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
