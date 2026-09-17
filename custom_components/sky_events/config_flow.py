"""Config and options flow for Sky Events."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AURORA_ALERT_ENTITY,
    CONF_AURORA_PROBABILITY_ENTITY,
    CONF_CLOUD_LIMIT,
    CONF_DETAILS_PATH,
    CONF_METEOR_MIN_RADIANT_ALTITUDE,
    CONF_METEOR_MIN_ZHR,
    CONF_METEOR_MOON_LIMIT,
    CONF_METEOR_WARN_DAYS,
    CONF_MOONRISE_AZIMUTH_MAX,
    CONF_MOONRISE_AZIMUTH_MIN,
    CONF_MOONRISE_CORRIDOR_ENABLED,
    CONF_POSSIBLE_KP,
    CONF_PROMISING_KP,
    CONF_STALE_HOURS,
    CONF_WEATHER_ENTITY,
    DEFAULT_CLOUD_LIMIT,
    DEFAULT_METEOR_MIN_RADIANT_ALTITUDE,
    DEFAULT_METEOR_MIN_ZHR,
    DEFAULT_METEOR_MOON_LIMIT,
    DEFAULT_METEOR_WARN_DAYS,
    DEFAULT_MOONRISE_AZIMUTH_MAX,
    DEFAULT_MOONRISE_AZIMUTH_MIN,
    DEFAULT_POSSIBLE_KP,
    DEFAULT_PROMISING_KP,
    DEFAULT_STALE_HOURS,
    DOMAIN,
)


def _percent() -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, max=100, step=1, unit_of_measurement="%")
    )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Thresholds, all live-adjustable.

    Kp thresholds are meaningful only relative to geomagnetic latitude, which
    is exactly why they are options rather than constants: a site further
    south needs a higher bar for the same chance of seeing anything.
    """
    def value(key: str, default: Any) -> Any:
        return current.get(key, default)

    return vol.Schema({
        vol.Optional(CONF_CLOUD_LIMIT, default=value(CONF_CLOUD_LIMIT, DEFAULT_CLOUD_LIMIT)):
            _percent(),
        vol.Optional(CONF_POSSIBLE_KP, default=value(CONF_POSSIBLE_KP, DEFAULT_POSSIBLE_KP)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=9, step=0.1)),
        vol.Optional(CONF_PROMISING_KP, default=value(CONF_PROMISING_KP, DEFAULT_PROMISING_KP)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=9, step=0.1)),
        vol.Optional(CONF_METEOR_MIN_ZHR, default=value(CONF_METEOR_MIN_ZHR, DEFAULT_METEOR_MIN_ZHR)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=200, step=1)),
        vol.Optional(
            CONF_METEOR_MIN_RADIANT_ALTITUDE,
            default=value(CONF_METEOR_MIN_RADIANT_ALTITUDE, DEFAULT_METEOR_MIN_RADIANT_ALTITUDE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=80, step=1, unit_of_measurement="°")
        ),
        vol.Optional(
            CONF_METEOR_MOON_LIMIT, default=value(CONF_METEOR_MOON_LIMIT, DEFAULT_METEOR_MOON_LIMIT)
        ): _percent(),
        vol.Optional(
            CONF_METEOR_WARN_DAYS, default=value(CONF_METEOR_WARN_DAYS, DEFAULT_METEOR_WARN_DAYS)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=7, max=365, step=1, unit_of_measurement="days")
        ),
        vol.Optional(
            CONF_MOONRISE_CORRIDOR_ENABLED, default=value(CONF_MOONRISE_CORRIDOR_ENABLED, False)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_MOONRISE_AZIMUTH_MIN,
            default=value(CONF_MOONRISE_AZIMUTH_MIN, DEFAULT_MOONRISE_AZIMUTH_MIN),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=360, step=1, unit_of_measurement="°")
        ),
        vol.Optional(
            CONF_MOONRISE_AZIMUTH_MAX,
            default=value(CONF_MOONRISE_AZIMUTH_MAX, DEFAULT_MOONRISE_AZIMUTH_MAX),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=360, step=1, unit_of_measurement="°")
        ),
        vol.Optional(CONF_STALE_HOURS, default=value(CONF_STALE_HOURS, DEFAULT_STALE_HOURS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=72, step=1)),
        vol.Optional(CONF_DETAILS_PATH, default=value(CONF_DETAILS_PATH, "")):
            selector.TextSelector(),
    })


class SkyEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Location comes from Home Assistant itself; only sources are asked for."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # One entry per installation: the observer is this HA's own location,
        # so a second entry would be a duplicate of the first.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if self.hass.config.latitude is None or self.hass.config.longitude is None:
            return self.async_abort(reason="no_location")

        if user_input is not None:
            return self.async_create_entry(title="Sky Events", data=user_input)

        schema = vol.Schema({
            vol.Required(CONF_WEATHER_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather")
            ),
            vol.Optional(CONF_AURORA_PROBABILITY_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(CONF_AURORA_ALERT_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            vol.Optional(CONF_DETAILS_PATH, default=""): selector.TextSelector(),
        })
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={
                "latitude": f"{self.hass.config.latitude:.4f}",
                "longitude": f"{self.hass.config.longitude:.4f}",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> SkyEventsOptionsFlow:
        return SkyEventsOptionsFlow()


class SkyEventsOptionsFlow(OptionsFlow):
    """Thresholds, adjustable without a restart."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # A promising bar below the possible bar makes the ladder
            # incoherent: a Kp between them would report "Unlikely" despite
            # exceeding "promising". The original had no such check.
            if float(user_input[CONF_PROMISING_KP]) < float(user_input[CONF_POSSIBLE_KP]):
                errors[CONF_PROMISING_KP] = "promising_below_possible"
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current), errors=errors
        )
