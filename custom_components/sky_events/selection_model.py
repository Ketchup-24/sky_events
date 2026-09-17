"""Pure selection and notification-edge logic for the sky-event system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


UTC = timezone.utc
PACIFIC = ZoneInfo("America/Vancouver")


@dataclass(frozen=True)
class SkyEvent:
    event_id: str
    event_type: str
    subtype: str
    title: str
    start: datetime | None
    peak: datetime | None
    end: datetime | None
    importance: int
    explanation: str
    action: str
    icon: str
    cloud_cover: float | None = None
    safety: str | None = None
    details_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def meteor_qualifies(zhr: float, exceptional_outburst: bool, radiant_altitude: float, minimum_zhr: float, minimum_altitude: float) -> bool:
    """Keep ordinary low-rate showers out unless a source flags an outburst."""
    return exceptional_outburst or (zhr >= minimum_zhr and radiant_altitude >= minimum_altitude)


def meteor_dataset_freshness(expires_after: datetime, now: datetime) -> str:
    return "fresh" if now.astimezone(UTC) <= expires_after.astimezone(UTC) else "expired"


def meteor_dataset_days_remaining(expires_after: datetime, now: datetime) -> int:
    """Whole days until the meteor dataset stops being usable (negative once past)."""
    delta = expires_after.astimezone(UTC) - now.astimezone(UTC)
    return int(delta.total_seconds() // 86400)


def meteor_dataset_alert_stage(expires_after: datetime, now: datetime, warn_days: int = 60) -> str | None:
    """Which one-time maintenance reminder the dataset currently warrants.

    The dataset is a hand-maintained subset of the annually issued IMO
    calendar and deliberately expires rather than silently extrapolating.
    Without a reminder that expiry is invisible: meteor events simply stop
    appearing and nothing says why. `expiring` gives enough notice to fetch
    the next calendar before coverage lapses; `expired` reports that it has.
    Returns None when no reminder is due. The caller persists which stages
    it has already sent, keyed by the dataset's own expiry date so that
    updating the dataset re-arms both reminders.
    """
    remaining = meteor_dataset_days_remaining(expires_after, now)
    if remaining < 0:
        return "expired"
    if remaining <= warn_days:
        return "expiring"
    return None


def meteor_viewing_assessment(
    dark: bool, radiant_altitude: float, cloud_cover: float | None, cloud_limit: float,
    moon_illumination: float, moon_altitude: float, minimum_altitude: float,
    moon_limit: float = 0.60, moon_notice: float = 0.25,
) -> str:
    """Grade a meteor viewing window.

    `moon_limit` is the caller's configured Moon-interference ceiling. It used
    to be hardcoded at 0.60 here while the caller separately applied its own
    limit afterwards, which meant raising the user's limit above 60% had no
    effect at all - this function had already downgraded the window. The
    caller now passes its value in and applies no second check.
    """
    if not dark or radiant_altitude < minimum_altitude:
        return "Poor conditions"
    if cloud_cover is None:
        return "Unavailable"
    if cloud_cover > cloud_limit:
        return "Poor conditions"
    if moon_altitude > 0 and moon_illumination >= moon_limit:
        return "Poor conditions"
    if moon_altitude > 0 and moon_illumination >= min(moon_notice, moon_limit):
        return "Good viewing tonight — some moon interference"
    return "Good viewing tonight"


def event_status(event: SkyEvent, now: datetime, timezone_name: ZoneInfo = PACIFIC) -> str:
    now = now.astimezone(UTC)
    start = event.start.astimezone(UTC) if event.start else None
    end = event.end.astimezone(UTC) if event.end else None
    peak = event.peak.astimezone(UTC) if event.peak else None
    if start is None and peak is None and end is None:
        return "later"
    if start and end and start <= now <= end:
        return "active"
    if end and end < now:
        return "ended"
    local_now = now.astimezone(timezone_name)
    local_peak = (peak or start or now).astimezone(timezone_name)
    if local_peak.date() == local_now.date() or (
        local_now.hour >= 12 and local_peak.date() == local_now.date() + timedelta(days=1)
    ):
        return "tonight"
    if peak and peak - now <= timedelta(days=14):
        return "upcoming"
    return "later"


def _class_rank(status: str) -> int:
    return {"active": 4, "tonight": 3, "upcoming": 2, "later": 1}.get(status, 0)


def select_event(
    events: Iterable[SkyEvent],
    aurora: SkyEvent,
    now: datetime,
    timezone_name: ZoneInfo = PACIFIC,
) -> tuple[SkyEvent, str, list[SkyEvent]]:
    """Select by display window first, then explicit importance within it.

    `timezone_name` must be the site's own zone: event_status uses it to
    decide "tonight". This previously defaulted to PACIFIC here and was never
    passed by the caller, so a site outside America/Vancouver silently
    decided "tonight" in Vancouver local time.

    Status is evaluated once per event rather than three times, so a single
    selection pass cannot disagree with itself.
    """
    scored = [(event, event_status(event, now, timezone_name)) for event in events]
    candidates = [pair for pair in scored if pair[1] != "ended"]
    candidates.append((aurora, event_status(aurora, now, timezone_name)))
    ranked = sorted(
        candidates,
        key=lambda pair: (_class_rank(pair[1]), pair[0].importance),
        reverse=True,
    )
    selected, status = ranked[0]
    return selected, status, [event for event, _ in ranked[1:4]]


def visible_moonrise(
    illumination: float,
    sunset_delta_minutes: float,
    cloud_cover: float | None,
    cloud_limit: float,
    azimuth: float,
    corridor_enabled: bool,
    azimuth_min: float,
    azimuth_max: float,
) -> bool:
    if illumination < 0.95 or abs(sunset_delta_minutes) > 60 or cloud_cover is None or cloud_cover > cloud_limit:
        return False
    if not corridor_enabled:
        return True
    if azimuth_min <= azimuth_max:
        return azimuth_min <= azimuth <= azimuth_max
    return azimuth >= azimuth_min or azimuth <= azimuth_max


def eclipse_notification_stages(event: SkyEvent, now: datetime, cloud_usable: bool) -> list[str]:
    """Return eligible notification stages; persistence belongs to the caller."""
    if event.event_type not in {"solar_eclipse", "lunar_eclipse"} or not event.start or not event.peak:
        return []
    until = event.start.astimezone(UTC) - now.astimezone(UTC)
    if event.event_type == "solar_eclipse":
        if timedelta(days=25) <= until <= timedelta(days=35):
            return ["glasses"]
        if until <= timedelta(days=3) and until > timedelta(days=1) and cloud_usable:
            return ["three_days"]
        if until <= timedelta(days=1) and until > timedelta(hours=1):
            return ["morning_of"]
        if timedelta() <= until <= timedelta(hours=1):
            return ["final"]
    else:
        if until <= timedelta(days=3) and until > timedelta(days=1):
            return ["three_days"]
        if until <= timedelta(days=1) and until > timedelta(hours=1):
            return ["day_of"]
        if timedelta() <= until <= timedelta(hours=1) and cloud_usable:
            return ["one_hour"]
    return []


def meteor_notification_stages(event: SkyEvent, now: datetime, conditions_good: bool) -> list[str]:
    """Meteor stages are edge candidates; caller persists sent keys."""
    if event.event_type != "meteor_shower" or not event.start or not event.peak:
        return []
    until_peak = event.peak.astimezone(UTC) - now.astimezone(UTC)
    if event.start.astimezone(UTC) <= now.astimezone(UTC) <= event.end.astimezone(UTC) and conditions_good:
        return ["peak_now"]
    if timedelta(days=2, hours=12) <= until_peak <= timedelta(days=3, hours=12) and conditions_good:
        return ["three_days"]
    if timedelta() < until_peak <= timedelta(hours=18) and conditions_good:
        return ["day_of"]
    return []


def notification_stage_committed(recipients_available: bool, submission_succeeded: bool) -> bool:
    """Only consume a one-time stage after a real notification submission."""
    return recipients_available and submission_succeeded
