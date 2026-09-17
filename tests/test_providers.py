"""Tests for the framework-free astronomy providers.

These need `astronomy-engine` (the integration's own requirement) but nothing
from Home Assistant, which is the point of keeping providers.py free of
framework imports.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

# providers.py uses relative imports, which is correct for a Home Assistant
# integration but means it needs a package context. Build a synthetic package
# pointing at the component directory rather than importing the real
# custom_components.sky_events package, whose __init__.py pulls in Home
# Assistant itself - these tests deliberately need only astronomy-engine.
import importlib  # noqa: E402
import types  # noqa: E402

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "sky_events"
_PKG = "sky_events_under_test"
if _PKG not in sys.modules:
    _package = types.ModuleType(_PKG)
    _package.__path__ = [str(_COMPONENT)]
    sys.modules[_PKG] = _package

providers = importlib.import_module(f"{_PKG}.providers")
SkyEventProviders = providers.SkyEventProviders
Thresholds = providers.Thresholds
meteor_status = providers.meteor_status
nearest_cloud = providers.nearest_cloud

UTC = timezone.utc
TZ = ZoneInfo("America/Vancouver")
DATA = Path(__file__).resolve().parents[1] / "custom_components" / "sky_events" / "data" / "meteor_showers_2026_2027.json"


class NearestCloudTest(unittest.TestCase):
    """The matching window is load-bearing: a gap must not read as certainty."""

    BASE = datetime(2026, 9, 10, 4, tzinfo=UTC)

    def test_no_samples_is_unknown(self):
        self.assertIsNone(nearest_cloud(self.BASE, []))

    def test_closest_sample_wins(self):
        clouds = [(self.BASE - timedelta(minutes=30), 80.0), (self.BASE + timedelta(minutes=5), 20.0)]
        self.assertEqual(nearest_cloud(self.BASE, clouds), 20.0)

    def test_sample_inside_the_window_is_accepted(self):
        self.assertEqual(nearest_cloud(self.BASE, [(self.BASE + timedelta(minutes=100), 40.0)]), 40.0)

    def test_sample_outside_the_window_is_rejected(self):
        self.assertIsNone(nearest_cloud(self.BASE, [(self.BASE + timedelta(minutes=101), 40.0)]))


class MeteorStatusTest(unittest.TestCase):
    """Extracted from a triple-nested ternary so it can actually be tested."""

    NOW = datetime(2026, 8, 12, 6, tzinfo=UTC)

    def status(self, assessment, best_offset_hours, now=None):
        best = self.NOW + timedelta(hours=best_offset_hours)
        return meteor_status(
            assessment, best - timedelta(hours=1), best + timedelta(hours=1),
            best, now or self.NOW, TZ,
        )

    def test_good_inside_the_window_is_peak_now(self):
        self.assertEqual(self.status("Good viewing tonight", 0), "Peak now")

    def test_poor_conditions_are_reported_verbatim(self):
        self.assertEqual(self.status("Poor conditions", 0), "Poor conditions")

    def test_unavailable_is_reported_verbatim(self):
        self.assertEqual(self.status("Unavailable", 0), "Unavailable")

    def test_moon_interference_wording_is_preserved(self):
        assessment = "Good viewing tonight — some moon interference"
        self.assertEqual(self.status(assessment, 10), assessment)

    def test_good_on_a_later_local_date_is_upcoming(self):
        self.assertEqual(self.status("Good viewing tonight", 48), "Upcoming")


class DatasetExpiryTest(unittest.TestCase):
    """A date-only expiry means end of that day, not midnight at its start."""

    def test_date_only_expiry_covers_the_whole_day(self):
        expiry = SkyEventProviders.dataset_expiry({"expires_after": "2027-12-31"})
        self.assertEqual(expiry.date().isoformat(), "2027-12-31")
        self.assertGreater(expiry.hour, 22)

    def test_missing_or_malformed_expiry_is_none(self):
        self.assertIsNone(SkyEventProviders.dataset_expiry(None))
        self.assertIsNone(SkyEventProviders.dataset_expiry({}))
        self.assertIsNone(SkyEventProviders.dataset_expiry({"expires_after": "not-a-date"}))


class ShippedDatasetTest(unittest.TestCase):
    """Guard rails on the hand-maintained dataset that ships with the repo."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(DATA.read_text())
        cls.rows = cls.data["showers"]

    def test_declares_its_own_provenance_and_expiry(self):
        self.assertIn("expires_after", self.data)
        self.assertIn("supported_years", self.data)
        self.assertTrue((self.data.get("source") or {}).get("publisher"))

    def test_every_row_has_the_fields_the_provider_reads(self):
        for row in self.rows:
            for key in ("year", "id", "name", "peak_start_utc", "peak_end_utc", "zhr", "ra_hours", "dec_degrees"):
                self.assertIn(key, row, f"{row.get('id')} is missing {key}")

    def test_peak_windows_are_ordered(self):
        for row in self.rows:
            start = datetime.fromisoformat(row["peak_start_utc"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["peak_end_utc"].replace("Z", "+00:00"))
            self.assertLess(start, end, f"{row['id']} {row['year']} has a non-positive peak window")

    def test_radiant_coordinates_are_in_range(self):
        for row in self.rows:
            self.assertTrue(0 <= float(row["ra_hours"]) < 24, f"{row['id']} RA out of range")
            self.assertTrue(-90 <= float(row["dec_degrees"]) <= 90, f"{row['id']} Dec out of range")

    @unittest.expectedFailure
    def test_no_two_years_share_a_peak_clock_time(self):
        """KNOWN DATA DEFECT - expected to fail until the dataset is replaced.

        Solar-longitude peaks shift roughly +6h per common year, so identical
        clock times across years are astronomically impossible and mean the
        later year was extrapolated rather than quoted from a published
        calendar. The shipped file's own `maintenance` note forbids exactly
        that. Six of seven showers in 2027 duplicate their 2026 times:
        lyrids, eta_aquariids, perseids, orionids, leonids, geminids.

        Marked expectedFailure so the suite stays green while the defect is
        documented. When the dataset is rebuilt from the real IMO calendar
        this will report an unexpected success - at which point delete this
        decorator.
        """
        by_id: dict[str, dict[int, str]] = {}
        for row in self.rows:
            by_id.setdefault(row["id"], {})[row["year"]] = row["peak_start_utc"]
        duplicated = [
            shower for shower, years in by_id.items()
            if len({value[10:] for value in years.values()}) == 1 and len(years) > 1
        ]
        self.assertEqual(
            duplicated, [],
            "these showers carry identical clock times across years, which is "
            "astronomically impossible and indicates extrapolated data: "
            f"{duplicated}",
        )


class ProviderSmokeTest(unittest.TestCase):
    """The providers must run for a real observer without raising."""

    def setUp(self):
        self.providers = SkyEventProviders(
            latitude=49.5325, longitude=-124.6764, elevation=2, tz=TZ, data_path=DATA
        )
        self.now = datetime(2026, 9, 17, 6, tzinfo=UTC)
        self.thresholds = Thresholds()

    def test_eclipse_search_is_bounded_and_returns_events(self):
        events = self.providers.eclipse_events(self.now, [], self.thresholds)
        self.assertIsInstance(events, list)
        for event in events:
            self.assertIn(event.event_type, {"solar_eclipse", "lunar_eclipse"})
            self.assertIsNotNone(event.peak)

    def test_moon_event_returns_an_event_or_none(self):
        event = self.providers.moon_event(self.now, [], self.thresholds)
        self.assertTrue(event is None or event.event_type == "moon")

    def test_meteor_event_reports_freshness(self):
        _, freshness = self.providers.meteor_event(self.now, [], self.thresholds)
        self.assertIn(freshness, {"fresh", "expired", "unavailable"})

    def test_missing_dataset_is_unavailable_not_a_crash(self):
        broken = SkyEventProviders(
            latitude=49.5, longitude=-124.7, elevation=2, tz=TZ,
            data_path=Path("/nonexistent/meteors.json"),
        )
        event, freshness = broken.meteor_event(self.now, [], self.thresholds)
        self.assertIsNone(event)
        self.assertEqual(freshness, "unavailable")

    def test_one_malformed_row_does_not_lose_the_other_showers(self):
        """A single bad row used to raise into a blanket handler that dropped
        eclipses and moon events too."""
        data = json.loads(DATA.read_text())
        data["showers"] = [
            {"year": 2026, "id": "broken", "name": "Broken", "peak_start_utc": "nonsense",
             "peak_end_utc": "nonsense", "zhr": 100, "ra_hours": 3, "dec_degrees": 58},
            *data["showers"],
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(data, handle)
            path = Path(handle.name)
        try:
            provider = SkyEventProviders(
                latitude=49.5325, longitude=-124.6764, elevation=2, tz=TZ, data_path=path
            )
            _, freshness = provider.meteor_event(self.now, [], self.thresholds)
            self.assertEqual(freshness, "fresh", "a malformed row must not abort the provider")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
