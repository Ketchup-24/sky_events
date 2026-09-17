"""The Sky Events integration.

Publishes a qualitative aurora outlook and a single "most noteworthy sky
event" (eclipse, full-moon rise, meteor shower, aurora) for this Home
Assistant's own location, and fires a bus event once per notable stage.

It deliberately sends no notifications of its own - see README.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, PLATFORMS
from .coordinator import SkyEventsCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sky Events from a config entry."""
    if hass.config.latitude is None or hass.config.longitude is None:
        raise ConfigEntryNotReady(
            "Home Assistant has no location configured; set it under Settings > System > General"
        )

    coordinator = SkyEventsCoordinator(hass, entry)
    await coordinator.async_load_stored()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed: reload so new thresholds take effect immediately."""
    await hass.config_entries.async_reload(entry.entry_id)
