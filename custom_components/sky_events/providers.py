"""Local astronomy providers: eclipses, full-moon rises and meteor showers.

Deliberately free of Home Assistant imports. Everything here is
CPU-bound (Astronomy Engine runs iterative numerical searches), so the caller
runs it in an executor and can unit-test it directly.

Each provider returns SkyEvent candidates; ranking and display-window
selection belong to selection_model.select_event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import astronomy as astro

from .selection_model import (
    SkyEvent,
    meteor_dataset_freshness,
    meteor_qualifies,
    meteor_viewing_assessment,
    visible_moonrise,
)

_LOGGER = logging.getLogger(__name__)

UTC = timezone.utc

# Nautical twilight. Used by every provider so one definition of "dark" holds
# across aurora, meteors and the viewing assessment.
DARKNESS_ELEVATION = -12.0

# Meteor slot scoring. A higher radiant is better; cloud and an illuminated
# Moon above the horizon are penalties. These weights encode a viewing model
# rather than a physical law, which is why they are named and tunable here
# rather than buried in an expression.
MOON_PENALTY_WEIGHT = 35.0
CLOUD_PENALTY_WEIGHT = 0.45
SLOT_MINUTES = 30

SUPERMOON_DISTANCE_KM = 360_000
MOON_LOOKAHEAD_DAYS = 14
MOONRISE_WINDOW_MINUTES = 60

SOLAR_LEAD_DAYS = 35
LUNAR_LEAD_DAYS = 3
SOLAR_MIN_OBSCURATION = 0.10
SOLAR_SUBSTANTIAL_OBSCURATION = 0.50
LUNAR_MIN_OBSCURATION = 0.50

# Guard against a Next*Eclipse call that fails to advance. Without a cap that
# would be an infinite loop inside an update.
MAX_ECLIPSE_ITERATIONS = 40

NASA_ECLIPSE_SOURCE = "https://eclipse.gsfc.nasa.gov/"
SOLAR_SAFETY = (
    "Use certified solar-eclipse glasses or an approved solar filter. "
    "Regular sunglasses are not safe."
)


@dataclass(frozen=True)
class Thresholds:
    """Site-tunable limits. Kp lives with the aurora model, not here."""

    cloud_limit: float = 60
    meteor_min_zhr: float = 15
    meteor_min_radiant_altitude: float = 20
    meteor_moon_limit: float = 0.60
    moonrise_corridor_enabled: bool = False
    moonrise_azimuth_min: float = 0
    moonrise_azimuth_max: float = 360
    eclipse_horizon_days: int = 400


def to_astro_time(value: datetime) -> Any:
    value = value.astimezone(UTC)
    return astro.Time.Make(
        value.year, value.month, value.day, value.hour, value.minute,
        value.second + value.microsecond / 1_000_000,
    )


def from_astro_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def moon_illumination_fraction(at: Any) -> float:
    """0.0 (new) to 1.0 (full) from the Moon's phase angle."""
    return (1 + math.cos(math.radians(astro.MoonPhase(at)))) / 2


def nearest_cloud(
    at: datetime, clouds: Iterable[tuple[datetime, float]], max_offset_minutes: int = 100,
) -> float | None:
    """Cloud cover nearest `at`, or None if no sample is close enough.

    The window matters: accepting an arbitrarily distant sample would silently
    turn a gap in the forecast into a confident answer.
    """
    candidates = [(abs((when - at).total_seconds()), cover) for when, cover in clouds]
    if not candidates:
        return None
    delta, cover = min(candidates, key=lambda item: item[0])
    return cover if delta <= max_offset_minutes * 60 else None


def meteor_status(
    assessment: str, window_start: datetime, window_end: datetime,
    best_time: datetime, now: datetime, tz: ZoneInfo,
) -> str:
    """Short status word for the card.

    Extracted from a triple-nested conditional expression inside a dict
    literal, which was both unreviewable and untestable.
    """
    good = assessment.startswith("Good")
    if good and window_start <= now <= window_end:
        return "Peak now"
    if assessment != "Good viewing tonight":
        return assessment
    if best_time.astimezone(tz).date() == now.astimezone(tz).date():
        return "Good viewing tonight"
    return "Upcoming"


class SkyEventProviders:
    """Computes local sky-event candidates for one observer."""

    def __init__(
        self, latitude: float, longitude: float, elevation: float,
        tz: ZoneInfo, data_path: Path,
    ) -> None:
        self.observer = astro.Observer(latitude, longitude, elevation)
        self.tz = tz
        self.data_path = data_path

    # ---------- shared helpers ----------

    def _horizon(self, body: Any, at: Any) -> tuple[float, float]:
        equator = astro.Equator(body, at, self.observer, True, True)
        horizon = astro.Horizon(at, self.observer, equator.ra, equator.dec, astro.Refraction.Normal)
        return horizon.altitude, horizon.azimuth

    def _radiant_altitude(self, ra_hours: float, dec_degrees: float, at: Any) -> float:
        horizon = astro.Horizon(at, self.observer, ra_hours, dec_degrees, astro.Refraction.Normal)
        return horizon.altitude

    def _is_dark(self, at: Any) -> bool:
        return self._horizon(astro.Body.Sun, at)[0] <= DARKNESS_ELEVATION

    # ---------- meteor dataset ----------

    def load_meteor_dataset(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.data_path.read_text())
        except (FileNotFoundError, OSError, ValueError) as err:
            _LOGGER.warning("Meteor dataset unavailable: %s", err)
            return None

    @staticmethod
    def dataset_expiry(dataset: dict[str, Any] | None) -> datetime | None:
        if not dataset:
            return None
        raw = dataset.get("expires_after")
        if not raw:
            return None
        try:
            # Date-only values mean end of that day; a full timestamp is used
            # as given rather than having a time appended to it.
            parsed = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            if len(str(raw)) <= 10:
                parsed = datetime.combine(parsed.date(), datetime.max.time())
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    # ---------- providers ----------

    def meteor_event(
        self, now: datetime, clouds: list[tuple[datetime, float]], thresholds: Thresholds,
    ) -> tuple[SkyEvent | None, str, datetime | None]:
        """Best current meteor candidate, its dataset freshness, and the
        dataset's expiry.

        The expiry is returned rather than re-derived by the caller so the
        dataset file is read exactly once per update - and on the caller's
        executor thread, never on the event loop.
        """
        dataset = self.load_meteor_dataset()
        expiry = self.dataset_expiry(dataset)
        if dataset is None or expiry is None:
            return None, "unavailable", expiry
        freshness = meteor_dataset_freshness(expiry, now)
        if freshness != "fresh":
            return None, freshness, expiry

        local_year = now.astimezone(self.tz).year
        showers = [
            row for row in dataset.get("showers", [])
            if row.get("year") in {local_year, local_year + 1}
        ]
        source_urls = (dataset.get("source") or {}).get("urls") or {}

        best: SkyEvent | None = None
        for shower in showers:
            # Per-shower isolation: one malformed row in an annually
            # hand-maintained file must not take out eclipses and moon events
            # as well, which a single surrounding try/except would allow.
            try:
                candidate = self._one_meteor_event(shower, now, clouds, thresholds, source_urls, freshness)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipping malformed meteor shower row %s: %s", shower.get("id"), err)
                continue
            if candidate and (best is None or candidate.importance > best.importance):
                best = candidate
        return best, freshness, expiry

    def _one_meteor_event(
        self, shower: dict[str, Any], now: datetime, clouds: list[tuple[datetime, float]],
        thresholds: Thresholds, source_urls: dict[str, Any], freshness: str,
    ) -> SkyEvent | None:
        start = _parse_dt(shower.get("peak_start_utc"))
        end = _parse_dt(shower.get("peak_end_utc"))
        if not start or not end or end <= start:
            return None
        if now > end + timedelta(hours=2) or start - now > timedelta(days=3):
            return None

        peak_midpoint = start + (end - start) / 2
        ra_hours = float(shower["ra_hours"])
        dec_degrees = float(shower["dec_degrees"])

        slots: list[tuple[float, datetime, float, float | None, float, float, bool]] = []
        cursor = start
        while cursor <= end:
            at = to_astro_time(cursor)
            dark = self._is_dark(at)
            radiant_altitude = self._radiant_altitude(ra_hours, dec_degrees, at)
            moon_altitude, _ = self._horizon(astro.Body.Moon, at)
            illumination = moon_illumination_fraction(at)
            cloud = nearest_cloud(cursor, clouds)
            moon_penalty = illumination * MOON_PENALTY_WEIGHT if moon_altitude > 0 else 0
            cloud_penalty = (100 if cloud is None else cloud) * CLOUD_PENALTY_WEIGHT
            score = radiant_altitude - cloud_penalty - moon_penalty
            slots.append((score, cursor, radiant_altitude, cloud, illumination, moon_altitude, dark))
            cursor += timedelta(minutes=SLOT_MINUTES)

        if not slots:
            return None
        # Prefer the best *dark* slot. Scoring every slot and taking the max
        # meant that when a whole peak window fell in daylight the reported
        # conditions came from the earliest slot rather than the best one.
        dark_slots = [slot for slot in slots if slot[6]]
        if not dark_slots:
            return None
        _, best_time, radiant_altitude, cloud, illumination, moon_altitude, dark = max(
            dark_slots, key=lambda item: item[0]
        )

        if not meteor_qualifies(
            float(shower["zhr"]), bool(shower.get("exceptional_outburst")),
            radiant_altitude, thresholds.meteor_min_zhr, thresholds.meteor_min_radiant_altitude,
        ):
            return None

        assessment = meteor_viewing_assessment(
            dark, radiant_altitude, cloud, thresholds.cloud_limit, illumination,
            moon_altitude, thresholds.meteor_min_radiant_altitude,
            moon_limit=thresholds.meteor_moon_limit,
        )
        window_start = best_time - timedelta(hours=1)
        window_end = best_time + timedelta(hours=1)
        exceptional = bool(shower.get("exceptional_outburst"))
        viewable = assessment.startswith("Good")
        importance = (75 if exceptional else 40) if viewable else 5
        url = source_urls.get(str(shower.get("year")))
        moon_text = (
            f" at {moon_altitude:.0f}°." if moon_altitude > 0 else " and below the horizon."
        )

        return SkyEvent(
            event_id=f"meteor-{shower['id']}-{shower['year']}",
            event_type="meteor_shower",
            subtype="outburst" if exceptional else str(shower["id"]),
            title=f"{shower['name']} meteor shower",
            start=window_start, peak=best_time, end=window_end,
            importance=importance, cloud_cover=cloud, icon="mdi:meteor",
            explanation=(
                f"Published ideal ZHR {shower['zhr']} (not an expected personal count). "
                f"{assessment}. Radiant is {radiant_altitude:.0f}° high; "
                f"Moon is {illumination * 100:.0f}% illuminated{moon_text}"
            ),
            action="No telescope or binoculars needed; allow 20–30 minutes for night vision.",
            details_url=url,
            extra={
                "meteor_status": meteor_status(assessment, window_start, window_end, best_time, now, self.tz),
                "published_zhr": shower["zhr"],
                "zhr_note": "Ideal Zenithal Hourly Rate, not a personal expected rate.",
                "activity_start": shower.get("activity_start"),
                "activity_end": shower.get("activity_end"),
                "peak_utc": peak_midpoint.isoformat(),
                "radiant_altitude": round(radiant_altitude, 1),
                "moon_illumination": round(illumination, 3),
                "moon_altitude": round(moon_altitude, 1),
                "source_year": shower.get("year"),
                "source_url": url,
                "meteor_data_freshness": freshness,
            },
        )

    def moon_event(
        self, now: datetime, clouds: list[tuple[datetime, float]], thresholds: Thresholds,
    ) -> SkyEvent | None:
        phase = astro.SearchMoonQuarter(to_astro_time(now))
        guard = 0
        while phase.quarter != 2 and guard < 8:
            phase = astro.NextMoonQuarter(phase)
            guard += 1
        if phase.quarter != 2:
            return None
        full = from_astro_time(phase.time)
        if full - now > timedelta(days=MOON_LOOKAHEAD_DAYS):
            return None

        # Search from just before full moon; the rise nearest full moon is the
        # one being described, so anchor the search on it explicitly rather
        # than taking whichever rise a wider window happens to return first.
        rise_time = astro.SearchRiseSet(
            astro.Body.Moon, self.observer, astro.Direction.Rise, phase.time.AddDays(-1), 3
        )
        if rise_time is None:
            return None
        rise = from_astro_time(rise_time)
        sunset_time = astro.SearchRiseSet(
            astro.Body.Sun, self.observer, astro.Direction.Set, rise_time.AddDays(-0.6), 2
        )
        if sunset_time is None:
            return None
        sunset = from_astro_time(sunset_time)

        illumination = moon_illumination_fraction(rise_time)
        _, azimuth = self._horizon(astro.Body.Moon, rise_time)
        cloud = nearest_cloud(rise, clouds)
        delta_minutes = (rise - sunset).total_seconds() / 60

        if not visible_moonrise(
            illumination, delta_minutes, cloud, thresholds.cloud_limit, azimuth,
            thresholds.moonrise_corridor_enabled,
            thresholds.moonrise_azimuth_min, thresholds.moonrise_azimuth_max,
        ):
            return None

        distance_km = astro.Illumination(astro.Body.Moon, rise_time).geo_dist * astro.KM_PER_AU
        supermoon = distance_km < SUPERMOON_DISTANCE_KM
        # Signed, so "before sunset" and "after sunset" are distinguishable -
        # abs() discarded the very thing the window is about.
        when = "before" if delta_minutes < 0 else "after"

        return SkyEvent(
            event_id=f"full-moon-rise-{rise.date().isoformat()}",
            event_type="moon",
            subtype="supermoon" if supermoon else "full_moon_rise",
            title="Supermoon rise" if supermoon else "Noteworthy full-moon rise",
            start=rise, peak=rise, end=rise + timedelta(hours=2),
            importance=20 if supermoon else 15, cloud_cover=cloud, icon="mdi:moon-full",
            explanation=(
                f"{illumination * 100:.0f}% illuminated Moon rises "
                f"{abs(delta_minutes):.0f} minutes {when} sunset at azimuth {azimuth:.0f}°."
            ),
            action="Look east around moonrise",
            extra={
                "moonrise_azimuth": round(azimuth, 1),
                "illumination": round(illumination, 3),
                "distance_km": round(distance_km),
                "minutes_from_sunset": round(delta_minutes),
            },
        )

    def eclipse_events(
        self, now: datetime, clouds: list[tuple[datetime, float]], thresholds: Thresholds,
    ) -> list[SkyEvent]:
        events: list[SkyEvent] = []
        horizon = now + timedelta(days=thresholds.eclipse_horizon_days)

        solar = astro.SearchLocalSolarEclipse(to_astro_time(now), self.observer)
        for _ in range(MAX_ECLIPSE_ITERATIONS):
            peak = from_astro_time(solar.peak.time)
            if peak > horizon:
                break
            start = from_astro_time(solar.partial_begin.time)
            end = from_astro_time(solar.partial_end.time)
            if (
                solar.obscuration >= SOLAR_MIN_OBSCURATION
                and solar.peak.altitude > 0
                and now >= start - timedelta(days=SOLAR_LEAD_DAYS)
            ):
                kind = solar.kind.name.lower()
                if kind in {"total", "annular"}:
                    importance = 100
                elif solar.obscuration >= SOLAR_SUBSTANTIAL_OBSCURATION:
                    importance = 95
                else:
                    importance = 35
                events.append(SkyEvent(
                    event_id=f"solar-{peak.date().isoformat()}",
                    event_type="solar_eclipse", subtype=kind,
                    title=f"{kind.title()} solar eclipse",
                    start=start, peak=peak, end=end, importance=importance,
                    cloud_cover=nearest_cloud(peak, clouds), icon="mdi:weather-sunny-alert",
                    explanation=(
                        f"Local peak obscuration is {solar.obscuration:.3f}; "
                        f"Sun altitude {solar.peak.altitude:.0f}°."
                    ),
                    action="Prepare safe solar viewing", safety=SOLAR_SAFETY,
                    details_url=NASA_ECLIPSE_SOURCE,
                    extra={"magnitude": solar.obscuration, "sun_altitude": solar.peak.altitude},
                ))
            solar = astro.NextLocalSolarEclipse(solar.peak.time, self.observer)

        lunar = astro.SearchLunarEclipse(to_astro_time(now))
        for _ in range(MAX_ECLIPSE_ITERATIONS):
            peak = from_astro_time(lunar.peak)
            if peak > horizon:
                break
            half_width = timedelta(minutes=lunar.sd_partial or lunar.sd_penum)
            start, end = peak - half_width, peak + half_width
            altitude, _ = self._horizon(astro.Body.Moon, lunar.peak)
            kind = lunar.kind.name.lower()
            if (
                kind != "penumbral"
                and altitude > 0
                and (kind == "total" or lunar.obscuration >= LUNAR_MIN_OBSCURATION)
                and now >= start - timedelta(days=LUNAR_LEAD_DAYS)
            ):
                events.append(SkyEvent(
                    event_id=f"lunar-{peak.date().isoformat()}",
                    event_type="lunar_eclipse", subtype=kind,
                    title=f"{kind.title()} lunar eclipse",
                    start=start, peak=peak, end=end,
                    importance=90 if kind == "total" else 80,
                    cloud_cover=nearest_cloud(peak, clouds), icon="mdi:moon-waning-crescent",
                    explanation=(
                        f"Moon altitude at maximum is {altitude:.0f}°; "
                        f"umbral obscuration {lunar.obscuration:.2f}."
                    ),
                    action="Look for the eclipsed Moon", details_url=NASA_ECLIPSE_SOURCE,
                    extra={"magnitude": lunar.obscuration, "moon_altitude": altitude},
                ))
            lunar = astro.NextLunarEclipse(lunar.peak)

        return events


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
