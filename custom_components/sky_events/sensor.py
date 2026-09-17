"""Sensor entities for Sky Events."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SkyEventsCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SkyEventsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        AuroraOutlookSensor(coordinator, entry),
        NoteworthySkyEventSensor(coordinator, entry),
    ])


class SkyEventsBaseSensor(CoordinatorEntity[SkyEventsCoordinator], SensorEntity):
    """Shared device info and availability."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SkyEventsCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Sky Events",
            manufacturer="Sky Events",
            entry_type=DeviceEntryType.SERVICE,
        )


class AuroraOutlookSensor(SkyEventsBaseSensor):
    """Qualitative aurora outlook: Unlikely / Possible / Promising tonight / Look outside now."""

    _attr_name = "Aurora outlook"
    _attr_icon = "mdi:aurora"

    def __init__(self, coordinator: SkyEventsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "aurora_outlook")

    @property
    def native_value(self) -> str:
        return str((self.coordinator.data.aurora or {}).get("state", "Unavailable"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        aurora = data.aurora or {}
        peak_start = aurora.get("peak_start")
        peak_end = aurora.get("peak_end")
        return {
            "explanation": aurora.get("explanation"),
            "max_kp": aurora.get("max_kp"),
            "forecast_status": aurora.get("forecast_status"),
            "peak_start": peak_start.isoformat() if peak_start else None,
            "peak_end": peak_end.isoformat() if peak_end else None,
            "peak_cloud_cover": aurora.get("peak_cloud_cover"),
            "current_cloud_cover": aurora.get("current_cloud_cover"),
            "currently_dark": aurora.get("current_dark"),
            "cloud_limit": aurora.get("cloud_limit"),
            "live_probability": aurora.get("live_probability"),
            "live_alert": aurora.get("live_alert"),
            "night_key": aurora.get("night_key"),
            # Surfaced rather than hidden: an Unavailable outlook should say
            # which input is missing.
            "noaa_last_success": data.noaa_last_success.isoformat() if data.noaa_last_success else None,
            "noaa_error": data.noaa_error,
            "cloud_forecast_fresh": data.cloud_fresh,
            "source": "NOAA SWPC planetary Kp forecast",
        }


class NoteworthySkyEventSensor(SkyEventsBaseSensor):
    """The single most noteworthy sky event right now, across all providers."""

    _attr_name = "Noteworthy sky event"

    def __init__(self, coordinator: SkyEventsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "noteworthy_sky_event")

    @property
    def icon(self) -> str:
        selected = self.coordinator.data.selected
        return selected.icon if selected else "mdi:weather-night"

    @property
    def native_value(self) -> str | None:
        selected = self.coordinator.data.selected
        return selected.title if selected else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        selected = data.selected
        attrs: dict[str, Any] = {
            "meteor_data_freshness": data.meteor_freshness,
            "meteor_data_expires": data.meteor_expires,
            "meteor_data_days_remaining": data.meteor_days_remaining,
            "cloud_forecast_fresh": data.cloud_fresh,
            "secondary_events": [
                {"title": item.title, "type": item.event_type, "status": "candidate"}
                for item in data.secondary
            ],
        }
        if selected is None:
            return attrs
        # Provider extras merge first so they can never override a curated key.
        return {
            **selected.extra,
            **attrs,
            "event_id": selected.event_id,
            "event_type": selected.event_type,
            "event_subtype": selected.subtype,
            "status": data.status,
            "urgency_rank": data.status,
            "importance_rank": selected.importance,
            "start": selected.start.isoformat() if selected.start else None,
            "peak": selected.peak.isoformat() if selected.peak else None,
            "end": selected.end.isoformat() if selected.end else None,
            "cloud_cover": selected.cloud_cover,
            "explanation": selected.explanation,
            "action": selected.action,
            "safety_message": selected.safety,
            "details_url": selected.details_url or self.coordinator.details_path,
        }
