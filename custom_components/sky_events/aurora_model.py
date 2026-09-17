"""Pure NOAA/cloud/darkness logic for a site-local aurora outlook."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo


UTC = timezone.utc
PACIFIC = ZoneInfo("America/Vancouver")


@dataclass(frozen=True)
class KpPoint:
    """One three-hour NOAA planetary Kp interval."""

    start: datetime
    kp: float
    status: str

    @property
    def midpoint(self) -> datetime:
        return self.start + timedelta(minutes=90)


def parse_noaa_points(payload: Any) -> list[KpPoint]:
    """Validate NOAA's public forecast payload and normalize timestamps to UTC."""
    if not isinstance(payload, list):
        raise ValueError("NOAA payload is not a list")

    points: list[KpPoint] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        raw_time = row.get("time_tag")
        raw_kp = row.get("kp")
        status = str(row.get("observed") or "").lower()
        try:
            parsed = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            kp = float(raw_kp)
        except (TypeError, ValueError):
            continue
        if not 0 <= kp <= 9:
            continue
        points.append(KpPoint(parsed, kp, status))

    if not points:
        raise ValueError("NOAA payload has no valid Kp points")
    return sorted(set(points), key=lambda point: point.start)


def parse_hourly_clouds(forecast: Any) -> list[tuple[datetime, float]]:
    """Extract timestamped cloud-cover values from HA's weather forecast response."""
    if not isinstance(forecast, list):
        return []
    clouds: list[tuple[datetime, float]] = []
    for row in forecast:
        if not isinstance(row, dict) or "cloud_coverage" not in row:
            continue
        try:
            at = datetime.fromisoformat(str(row["datetime"]).replace("Z", "+00:00"))
            at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
            cloud = float(row["cloud_coverage"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= cloud <= 100:
            clouds.append((at, cloud))
    return sorted(clouds, key=lambda item: item[0])


def cloud_forecast_is_fresh(
    updated_at: datetime | None, now: datetime, max_age_hours: float,
) -> bool:
    """Require a recent HA hourly-forecast refresh before using cloud data."""
    if updated_at is None or max_age_hours <= 0:
        return False
    return now.astimezone(UTC) - updated_at.astimezone(UTC) <= timedelta(hours=max_age_hours)


def solar_elevation(at: datetime, latitude: float, longitude: float) -> float:
    """Approximate solar elevation (degrees) using NOAA's published equations."""
    at = at.astimezone(UTC)
    day_fraction = (
        at.hour * 3600 + at.minute * 60 + at.second + at.microsecond / 1_000_000
    ) / 86400
    gamma = 2 * math.pi / 365 * (at.timetuple().tm_yday - 1 + day_fraction - 0.5)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_minutes = (at.hour * 60 + at.minute + at.second / 60 + equation_of_time + 4 * longitude) % 1440
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    latitude_rad = math.radians(latitude)
    cosine_zenith = (
        math.sin(latitude_rad) * math.sin(declination)
        + math.cos(latitude_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cosine_zenith))))
    return 90 - zenith


def dark_enough(at: datetime, latitude: float, longitude: float, threshold: float = -12) -> bool:
    return solar_elevation(at, latitude, longitude) <= threshold


def _night_key(at: datetime, timezone_name: ZoneInfo) -> str:
    local = at.astimezone(timezone_name)
    anchor = local.date() if local.hour >= 12 else local.date() - timedelta(days=1)
    return anchor.isoformat()


def _nearest_cloud(at: datetime, clouds: Iterable[tuple[datetime, float]], max_offset_minutes: int = 100) -> float | None:
    candidates = [(abs((when - at).total_seconds()), cover) for when, cover in clouds]
    if not candidates:
        return None
    delta, cloud = min(candidates, key=lambda item: item[0])
    return cloud if delta <= max_offset_minutes * 60 else None


def notification_decision(
    prior: dict[str, Any], state: str, night_key: str | None,
) -> tuple[dict[str, Any], bool, bool]:
    """Return persisted notification state plus advance/live notification edges."""
    updated = {
        "advance_night": prior.get("advance_night"),
        "live_active": bool(prior.get("live_active", False)),
    }
    send_advance = state == "Promising tonight" and bool(night_key) and updated["advance_night"] != night_key
    if send_advance:
        updated["advance_night"] = night_key

    live_now = state == "Look outside now"
    send_live = live_now and not updated["live_active"]
    updated["live_active"] = live_now
    return updated, send_advance, send_live


def notification_delivery_decision(
    prior: dict[str, Any], state: str, night_key: str | None, recipients_available: bool,
) -> tuple[dict[str, Any], bool, bool]:
    """Keep notification edges pending until at least one recipient is enabled.

    A disabled alert setting must not consume the only advance/live edge.  A
    non-live state still clears a previously delivered live edge so a later
    genuine recovery can notify again.
    """
    updated, send_advance, send_live = notification_decision(prior, state, night_key)
    if recipients_available:
        return updated, send_advance, send_live

    retained = dict(prior)
    if state != "Look outside now":
        retained["live_active"] = False
    return retained, False, False


def evaluate_outlook(
    points: Iterable[KpPoint],
    clouds: Iterable[tuple[datetime, float]],
    now: datetime,
    *,
    latitude: float,
    longitude: float,
    timezone_name: ZoneInfo = PACIFIC,
    cloud_limit: float = 60,
    possible_kp: float = 5,
    promising_kp: float = 6,
    live_probability: float | None = None,
    live_alert: bool = False,
    dark_check: Callable[[datetime], bool] | None = None,
) -> dict[str, Any]:
    """Return one qualitative, locally timed outlook for the configured site."""
    now = now.astimezone(UTC)
    points = list(points)
    clouds = list(clouds)
    is_dark = dark_check or (lambda at: dark_enough(at, latitude, longitude))
    current_cloud = _nearest_cloud(now, clouds)
    dark_now = is_dark(now)

    local_now = now.astimezone(timezone_name)
    anchor_dates = [
        (local_now.date() - timedelta(days=1)) if local_now.hour < 12 else local_now.date(),
        local_now.date(),
        local_now.date() + timedelta(days=1),
    ]
    anchors = list(dict.fromkeys(anchor_dates))
    candidate_points: list[KpPoint] = []
    night_key = None
    for anchor in anchors:
        matching = [
            point
            for point in points
            if point.start + timedelta(hours=3) >= now
            and point.status != "observed"
            and _night_key(point.midpoint, timezone_name) == anchor.isoformat()
            and is_dark(point.midpoint)
        ]
        if matching:
            candidate_points = matching
            night_key = anchor.isoformat()
            break

    base = {
        "night_key": night_key,
        "current_dark": dark_now,
        "current_cloud_cover": current_cloud,
        "live_probability": live_probability,
        "live_alert": live_alert,
        "cloud_limit": cloud_limit,
    }
    if not candidate_points:
        return {**base, "state": "Unavailable", "explanation": "No NOAA Kp forecast interval overlaps the next local dark period."}

    peak = max(candidate_points, key=lambda point: (point.kp, -point.start.timestamp()))
    peak_cloud = _nearest_cloud(peak.midpoint, clouds)
    base.update(
        {
            "max_kp": peak.kp,
            "peak_start": peak.start.astimezone(timezone_name),
            "peak_end": (peak.start + timedelta(hours=3)).astimezone(timezone_name),
            "peak_cloud_cover": peak_cloud,
            "forecast_status": peak.status,
        }
    )
    if peak_cloud is None or current_cloud is None:
        return {**base, "state": "Unavailable", "explanation": "Hourly cloud-cover forecast is missing for the relevant viewing interval."}

    if live_alert and dark_now and current_cloud <= cloud_limit:
        return {
            **base,
            "state": "Look outside now",
            "explanation": "It is dark, the live NOAA Aurora integration is above its configured threshold, and forecast cloud cover is usable.",
        }
    if peak.kp < possible_kp:
        return {**base, "state": "Unlikely", "explanation": f"Maximum Kp {peak.kp:g} is below the Kp {possible_kp:g} watch threshold."}
    if peak.kp >= promising_kp and peak_cloud <= cloud_limit:
        return {**base, "state": "Promising tonight", "explanation": f"Kp {peak.kp:g} is forecast during darkness with {peak_cloud:.0f}% cloud cover."}
    if peak.kp >= promising_kp:
        return {**base, "state": "Possible", "explanation": f"Kp {peak.kp:g} is promising, but {peak_cloud:.0f}% cloud cover is the limiting factor."}
    return {**base, "state": "Possible", "explanation": f"Kp {peak.kp:g} may be photographically interesting, with {peak_cloud:.0f}% cloud cover."}
