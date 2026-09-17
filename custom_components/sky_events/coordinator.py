"""Update coordinator for Sky Events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .aurora_model import (
    KpPoint,
    evaluate_outlook,
    notification_decision,
    parse_hourly_clouds,
    parse_noaa_points,
)
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
    ECLIPSE_HORIZON_DAYS,
    EVENT_STAGE,
    NOAA_KP_URL,
    NOAA_REFRESH_HOURS,
    STORAGE_KEY,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from .providers import SkyEventProviders, Thresholds
from .selection_model import (
    SkyEvent,
    eclipse_notification_stages,
    meteor_dataset_alert_stage,
    meteor_dataset_days_remaining,
    meteor_notification_stages,
    select_event,
)

_LOGGER = logging.getLogger(__name__)

UTC = timezone.utc


@dataclass
class SkyEventsData:
    """Everything the entities render, computed once per update."""

    aurora: dict[str, Any] = field(default_factory=dict)
    selected: SkyEvent | None = None
    status: str = "unavailable"
    secondary: list[SkyEvent] = field(default_factory=list)
    cloud_updated: datetime | None = None
    cloud_fresh: bool = False
    meteor_freshness: str = "unavailable"
    meteor_expires: str | None = None
    meteor_days_remaining: int | None = None
    noaa_last_success: datetime | None = None
    noaa_error: str | None = None


class SkyEventsCoordinator(DataUpdateCoordinator[SkyEventsData]):
    """Fetches NOAA Kp, reads a cloud forecast, and computes local astronomy."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stages: set[str] = set()
        self._aurora_edges: dict[str, Any] = {"advance_night": None, "live_active": False}
        self._points: list[KpPoint] = []
        self._noaa_last_success: datetime | None = None
        self._noaa_error: str | None = None
        self._noaa_next_refresh: datetime | None = None

        self.tz = ZoneInfo(str(hass.config.time_zone or "UTC"))
        self.providers = SkyEventProviders(
            latitude=float(hass.config.latitude),
            longitude=float(hass.config.longitude),
            elevation=float(hass.config.elevation or 0),
            tz=self.tz,
            data_path=Path(__file__).parent / "data" / "meteor_showers_2026_2027.json",
        )

    # ---------- persisted one-time stages ----------

    async def async_load_stored(self) -> None:
        stored = await self._store.async_load() or {}
        self._stages = set(stored.get("stages", []))
        edges = stored.get("aurora_edges")
        if isinstance(edges, dict):
            self._aurora_edges.update(edges)

    async def _async_save(self) -> None:
        # Store gives atomic writes and delayed save, so a crash mid-write
        # cannot leave truncated JSON that reads back as "nothing notified"
        # and re-announces every pending stage.
        await self._store.async_save(
            {"stages": sorted(self._stages), "aurora_edges": self._aurora_edges}
        )

    # ---------- option helpers ----------

    def _option(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _thresholds(self) -> Thresholds:
        return Thresholds(
            cloud_limit=float(self._option(CONF_CLOUD_LIMIT, DEFAULT_CLOUD_LIMIT)),
            meteor_min_zhr=float(self._option(CONF_METEOR_MIN_ZHR, DEFAULT_METEOR_MIN_ZHR)),
            meteor_min_radiant_altitude=float(
                self._option(CONF_METEOR_MIN_RADIANT_ALTITUDE, DEFAULT_METEOR_MIN_RADIANT_ALTITUDE)
            ),
            meteor_moon_limit=float(
                self._option(CONF_METEOR_MOON_LIMIT, DEFAULT_METEOR_MOON_LIMIT)
            ) / 100,
            moonrise_corridor_enabled=bool(self._option(CONF_MOONRISE_CORRIDOR_ENABLED, False)),
            moonrise_azimuth_min=float(
                self._option(CONF_MOONRISE_AZIMUTH_MIN, DEFAULT_MOONRISE_AZIMUTH_MIN)
            ),
            moonrise_azimuth_max=float(
                self._option(CONF_MOONRISE_AZIMUTH_MAX, DEFAULT_MOONRISE_AZIMUTH_MAX)
            ),
            eclipse_horizon_days=ECLIPSE_HORIZON_DAYS,
        )

    @property
    def details_path(self) -> str | None:
        return self.entry.options.get(CONF_DETAILS_PATH) or self.entry.data.get(CONF_DETAILS_PATH)

    # ---------- inputs ----------

    async def _async_refresh_noaa(self, now: datetime) -> None:
        """Refresh the NOAA Kp forecast, at most every NOAA_REFRESH_HOURS."""
        if self._noaa_next_refresh and now < self._noaa_next_refresh:
            return
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(NOAA_KP_URL, timeout=20) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            self._points = parse_noaa_points(payload)
            self._noaa_last_success = now
            self._noaa_error = None
        except Exception as err:  # noqa: BLE001 - a bad fetch must not stop the update
            self._noaa_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning("NOAA Kp refresh failed: %s", self._noaa_error)
        finally:
            # Back off on failure too, so a persistent outage is not retried
            # on every five-minute tick.
            self._noaa_next_refresh = now + timedelta(hours=NOAA_REFRESH_HOURS)

    async def _async_cloud_forecast(self) -> tuple[list[tuple[datetime, float]], datetime | None]:
        """Hourly cloud cover, fetched straight from the weather integration.

        The AppDaemon original needed a template sensor to cache
        weather.get_forecasts and then guessed the cache's freshness from
        last_updated. Calling the service directly removes both the cache and
        the guesswork: the data is as fresh as this call.
        """
        entity_id = self._option(CONF_WEATHER_ENTITY, None)
        if not entity_id:
            return [], None
        try:
            response = await self.hass.services.async_call(
                "weather", "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": entity_id},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not fetch hourly forecast from %s: %s", entity_id, err)
            return [], None
        forecast = ((response or {}).get(entity_id) or {}).get("forecast") or []
        return parse_hourly_clouds(forecast), dt_util.utcnow()

    def _numeric_state(self, key: str) -> float | None:
        entity_id = self._option(key, None)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _is_on(self, key: str) -> bool:
        entity_id = self._option(key, None)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == "on"

    # ---------- update ----------

    async def _async_update_data(self) -> SkyEventsData:
        now = dt_util.utcnow()
        thresholds = self._thresholds()
        await self._async_refresh_noaa(now)

        clouds, cloud_updated = await self._async_cloud_forecast()
        cloud_fresh = bool(clouds)

        stale_hours = float(self._option(CONF_STALE_HOURS, DEFAULT_STALE_HOURS))
        source_stale = (
            self._noaa_last_success is None
            or now - self._noaa_last_success > timedelta(hours=stale_hours)
        )

        data = SkyEventsData(
            cloud_updated=cloud_updated, cloud_fresh=cloud_fresh,
            noaa_last_success=self._noaa_last_success, noaa_error=self._noaa_error,
        )

        # ---- aurora ----
        if source_stale:
            detail = "NOAA forecast is stale or has not been retrieved."
            if self._noaa_error:
                detail += f" Last error: {self._noaa_error}"
            data.aurora = {"state": "Unavailable", "explanation": detail}
        elif not cloud_fresh:
            data.aurora = {
                "state": "Unavailable",
                "explanation": "Hourly cloud forecast is unavailable.",
            }
        else:
            data.aurora = evaluate_outlook(
                self._points, clouds, now,
                latitude=self.hass.config.latitude,
                longitude=self.hass.config.longitude,
                timezone_name=self.tz,
                cloud_limit=thresholds.cloud_limit,
                possible_kp=float(self._option(CONF_POSSIBLE_KP, DEFAULT_POSSIBLE_KP)),
                promising_kp=float(self._option(CONF_PROMISING_KP, DEFAULT_PROMISING_KP)),
                live_probability=self._numeric_state(CONF_AURORA_PROBABILITY_ENTITY),
                live_alert=self._is_on(CONF_AURORA_ALERT_ENTITY),
            )

        # ---- local astronomy, off the event loop ----
        events: list[SkyEvent] = []
        meteor_freshness = "unavailable"
        try:
            events, meteor_freshness = await self.hass.async_add_executor_job(
                self._compute_local, now, clouds, thresholds
            )
        except Exception as err:  # noqa: BLE001 - aurora must survive an astronomy failure
            _LOGGER.warning("Local astronomy calculation failed: %s", err)
        data.meteor_freshness = meteor_freshness

        aurora_event = self._aurora_as_event(data.aurora, thresholds)
        data.selected, data.status, data.secondary = select_event(
            events, aurora_event, now, self.tz
        )

        # ---- meteor dataset maintenance ----
        expiry = self.providers.dataset_expiry(self.providers.load_meteor_dataset())
        if expiry is not None:
            data.meteor_expires = expiry.date().isoformat()
            data.meteor_days_remaining = meteor_dataset_days_remaining(expiry, now)
            warn_days = int(self._option(CONF_METEOR_WARN_DAYS, DEFAULT_METEOR_WARN_DAYS))
            stage = meteor_dataset_alert_stage(expiry, now, warn_days)
            if stage:
                await self._async_announce(
                    key=f"meteor-dataset:{data.meteor_expires}:{stage}",
                    payload={
                        "kind": "maintenance",
                        "event_type": "meteor_dataset",
                        "stage": stage,
                        "title": (
                            "Meteor shower data has expired" if stage == "expired"
                            else "Meteor shower data expires soon"
                        ),
                        "message": (
                            f"The meteor dataset expired on {data.meteor_expires}; meteor "
                            "showers are no longer reported. Update it from the current IMO "
                            "Meteor Shower Calendar and extend supported_years."
                            if stage == "expired" else
                            f"The meteor dataset expires in {data.meteor_days_remaining} day(s), "
                            f"on {data.meteor_expires}. Update it from the next IMO Meteor "
                            "Shower Calendar before meteor coverage lapses."
                        ),
                        "days_remaining": data.meteor_days_remaining,
                    },
                )

        await self._async_announce_event_stages(events, now, thresholds)
        await self._async_announce_aurora(data.aurora)
        return data

    def _compute_local(
        self, now: datetime, clouds: list[tuple[datetime, float]], thresholds: Thresholds,
    ) -> tuple[list[SkyEvent], str]:
        """Runs in an executor: Astronomy Engine searches are CPU-bound."""
        events = self.providers.eclipse_events(now, clouds, thresholds)
        moon = self.providers.moon_event(now, clouds, thresholds)
        if moon:
            events.append(moon)
        meteor, freshness = self.providers.meteor_event(now, clouds, thresholds)
        if meteor:
            events.append(meteor)
        return events, freshness

    def _aurora_as_event(self, aurora: dict[str, Any], thresholds: Thresholds) -> SkyEvent:
        state = str(aurora.get("state", "Unavailable"))
        importance = {"Look outside now": 50, "Promising tonight": 30, "Possible": 10}.get(state, 1)
        return SkyEvent(
            event_id=f"aurora-{dt_util.now().date().isoformat()}",
            event_type="aurora", subtype=state, title="Aurora outlook",
            start=aurora.get("peak_start"), peak=aurora.get("peak_start"),
            end=aurora.get("peak_end"),
            importance=importance,
            explanation=str(aurora.get("explanation", "")),
            action="Check the aurora outlook",
            icon="mdi:aurora",
            cloud_cover=aurora.get("current_cloud_cover"),
            details_url=self.details_path,
            extra={"aurora_state": state, "kp": aurora.get("max_kp")},
        )

    # ---------- stage announcements ----------

    async def _async_announce(self, key: str, payload: dict[str, Any]) -> None:
        """Fire a stage event once, ever.

        Firing a bus event is local and cannot partially fail, so unlike the
        AppDaemon original there is no notion of a stage being "consumed" by a
        delivery that never happened. Whether anyone is told is the listening
        automation's decision, not this integration's.
        """
        if key in self._stages:
            return
        self._stages.add(key)
        await self._async_save()
        self.hass.bus.async_fire(EVENT_STAGE, {"key": key, **payload})

    async def _async_announce_event_stages(
        self, events: list[SkyEvent], now: datetime, thresholds: Thresholds,
    ) -> None:
        for candidate in events:
            usable = (
                candidate.cloud_cover is not None
                and candidate.cloud_cover <= thresholds.cloud_limit
            )
            stages = eclipse_notification_stages(candidate, now, usable)
            if candidate.event_type == "meteor_shower":
                stages = meteor_notification_stages(
                    candidate, now,
                    candidate.extra.get("meteor_status") not in {"Poor conditions", "Unavailable"},
                )
            for stage in stages:
                await self._async_announce(
                    key=f"{candidate.event_id}:{stage}",
                    payload={
                        "kind": "sky_event",
                        "event_type": candidate.event_type,
                        "event_id": candidate.event_id,
                        "stage": stage,
                        "title": candidate.title,
                        "message": candidate.explanation,
                        "action": candidate.action,
                        "safety": candidate.safety,
                        "cloud_cover": candidate.cloud_cover,
                        "peak": candidate.peak.isoformat() if candidate.peak else None,
                        "details_url": candidate.details_url or self.details_path,
                    },
                )

    async def _async_announce_aurora(self, aurora: dict[str, Any]) -> None:
        state = str(aurora.get("state", "Unavailable"))
        night_key = aurora.get("night_key")
        updated, send_advance, send_live = notification_decision(
            self._aurora_edges, state, night_key
        )
        if updated != self._aurora_edges:
            self._aurora_edges = updated
            await self._async_save()
        for send, stage, title in (
            (send_advance, "advance", "Aurora promising tonight"),
            (send_live, "live", "Look outside now"),
        ):
            if not send:
                continue
            self.hass.bus.async_fire(EVENT_STAGE, {
                "kind": "aurora",
                "event_type": "aurora",
                "stage": stage,
                "title": title,
                "message": str(aurora.get("explanation", "")),
                "state": state,
                "night_key": night_key,
                "max_kp": aurora.get("max_kp"),
                "cloud_cover": aurora.get("peak_cloud_cover") or aurora.get("current_cloud_cover"),
                "details_url": self.details_path,
            })
