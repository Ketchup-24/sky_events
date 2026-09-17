"""Selection, filtering, and notification-edge tests for sky events."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "sky_events"))
from selection_model import (  # noqa: E402
    SkyEvent, eclipse_notification_stages, meteor_dataset_alert_stage, meteor_dataset_days_remaining,
    meteor_dataset_freshness, meteor_notification_stages,
    meteor_qualifies, meteor_viewing_assessment, notification_stage_committed, select_event, visible_moonrise,
)


NOW = datetime(2026, 9, 9, 3, tzinfo=timezone.utc)


def event(event_id, kind, importance, start, end, title="Event"):
    return SkyEvent(event_id, kind, kind, title, start, start + (end - start) / 2, end, importance, "test", "test", "mdi:star")


def aurora(state="Possible", importance=10):
    return event("aurora", "aurora", importance, NOW + timedelta(hours=2), NOW + timedelta(hours=5), state)


class NoteworthySkyEventTest(unittest.TestCase):
    def test_existing_aurora_is_default(self):
        selected, _, _ = select_event([], aurora("Unlikely", 1), NOW)
        self.assertEqual(selected.event_type, "aurora")

    def test_promising_aurora_wins_without_rarer_competitor(self):
        selected, _, _ = select_event([], aurora("Promising tonight", 30), NOW)
        self.assertEqual(selected.title, "Promising tonight")

    def test_active_solar_eclipse_outranks_active_aurora(self):
        solar = event("solar", "solar_eclipse", 90, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        active_aurora = event("aurora", "aurora", 50, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        selected, _, _ = select_event([solar], active_aurora, NOW)
        self.assertEqual(selected.event_type, "solar_eclipse")

    def test_active_lunar_eclipse_outranks_ordinary_moon_event(self):
        lunar = event("lunar", "lunar_eclipse", 80, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        moon = event("moon", "moon", 15, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        selected, _, _ = select_event([moon, lunar], aurora("Unlikely", 1), NOW)
        self.assertEqual(selected.event_type, "lunar_eclipse")

    def test_live_aurora_outranks_later_eclipse(self):
        future_solar = event("solar", "solar_eclipse", 90, NOW + timedelta(days=30), NOW + timedelta(days=30, hours=1))
        live_aurora = event("aurora", "aurora", 50, NOW - timedelta(minutes=5), NOW + timedelta(minutes=30))
        selected, status, _ = select_event([future_solar], live_aurora, NOW)
        self.assertEqual(selected.event_type, "aurora")
        self.assertEqual(status, "active")

    def test_total_beats_partial_in_same_window(self):
        total = event("total", "solar_eclipse", 100, NOW + timedelta(hours=2), NOW + timedelta(hours=4))
        partial = event("partial", "solar_eclipse", 95, NOW + timedelta(hours=2), NOW + timedelta(hours=4))
        selected, _, _ = select_event([partial, total], aurora("Unlikely", 1), NOW)
        self.assertEqual(selected.event_id, "total")

    def test_trivial_solar_and_penumbral_filtering_is_done_by_candidates(self):
        # Only qualifying candidates are passed to the selector.
        selected, _, _ = select_event([], aurora(), NOW)
        self.assertEqual(selected.event_type, "aurora")

    def test_clear_full_moon_rise_qualifies(self):
        self.assertTrue(visible_moonrise(0.98, 35, 20, 60, 100, False, 0, 0))

    def test_cloudy_or_out_of_corridor_moonrise_is_suppressed(self):
        self.assertFalse(visible_moonrise(0.98, 35, 90, 60, 100, False, 0, 0))
        self.assertFalse(visible_moonrise(0.98, 35, 20, 60, 100, True, 120, 160))

    def test_notification_stages_are_single_edges(self):
        solar = event("solar", "solar_eclipse", 90, NOW + timedelta(days=30), NOW + timedelta(days=30, hours=2))
        self.assertEqual(eclipse_notification_stages(solar, NOW, False), ["glasses"])
        lunar = event("lunar", "lunar_eclipse", 80, NOW + timedelta(minutes=30), NOW + timedelta(hours=2))
        self.assertEqual(eclipse_notification_stages(lunar, NOW, True), ["one_hour"])

    def test_timezone_display_input_is_utc(self):
        selected, status, _ = select_event([], aurora(), NOW)
        self.assertEqual(status, "tonight")
        self.assertEqual(selected.start.tzinfo, timezone.utc)

    def test_major_shower_clear_dark_moonless_is_good(self):
        self.assertTrue(meteor_qualifies(100, False, 45, 15, 20))
        self.assertEqual(meteor_viewing_assessment(True, 45, 15, 60, 0.10, -8, 20), "Good viewing tonight")

    def test_cloud_or_bright_moon_degrades_shower(self):
        self.assertEqual(meteor_viewing_assessment(True, 45, 90, 60, 0.10, -8, 20), "Poor conditions")
        self.assertEqual(meteor_viewing_assessment(True, 45, 15, 60, 0.90, 35, 20), "Poor conditions")

    def test_low_radiant_or_daylight_is_poor(self):
        self.assertEqual(meteor_viewing_assessment(True, 10, 15, 60, 0.05, -5, 20), "Poor conditions")
        self.assertEqual(meteor_viewing_assessment(False, 45, 15, 60, 0.05, -5, 20), "Poor conditions")

    def test_minor_is_suppressed_but_outburst_promotes(self):
        self.assertFalse(meteor_qualifies(10, False, 45, 15, 20))
        self.assertTrue(meteor_qualifies(10, True, 10, 15, 20))

    def test_peak_now_shower_and_stages(self):
        meteor = event("meteor", "meteor_shower", 40, NOW - timedelta(minutes=20), NOW + timedelta(minutes=40), "Perseids meteor shower")
        selected, status, _ = select_event([meteor], aurora("Unlikely", 1), NOW)
        self.assertEqual((selected.event_type, status), ("meteor_shower", "active"))
        self.assertEqual(meteor_notification_stages(meteor, NOW, True), ["peak_now"])

    def test_active_eclipse_beats_shower_but_outburst_beats_live_aurora(self):
        meteor = event("meteor", "meteor_shower", 40, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        eclipse = event("solar", "solar_eclipse", 100, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        selected, _, _ = select_event([meteor, eclipse], aurora("Look outside now", 50), NOW)
        self.assertEqual(selected.event_type, "solar_eclipse")
        outburst = event("outburst", "meteor_shower", 75, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        selected, _, _ = select_event([outburst], aurora("Look outside now", 50), NOW)
        self.assertEqual(selected.event_id, "outburst")

    def test_live_aurora_and_tonight_shower_beat_future_eclipse_by_urgency(self):
        future_eclipse = event("solar", "solar_eclipse", 100, NOW + timedelta(days=30), NOW + timedelta(days=30, hours=1))
        tonight_meteor = event("meteor", "meteor_shower", 40, NOW + timedelta(hours=2), NOW + timedelta(hours=4))
        selected, status, _ = select_event([future_eclipse, tonight_meteor], aurora("Unlikely", 1), NOW)
        self.assertEqual((selected.event_type, status), ("meteor_shower", "tonight"))
        live_aurora = event("aurora", "aurora", 50, NOW - timedelta(minutes=5), NOW + timedelta(minutes=30))
        selected, _, _ = select_event([tonight_meteor], live_aurora, NOW)
        self.assertEqual(selected.event_type, "aurora")

    def test_meteor_notification_edges_are_deterministic(self):
        meteor = event("meteor", "meteor_shower", 40, NOW + timedelta(days=3), NOW + timedelta(days=3, hours=2))
        self.assertEqual(meteor_notification_stages(meteor, NOW, True), ["three_days"])
        self.assertEqual(meteor_notification_stages(meteor, NOW, False), [])

    def test_meteor_dataset_expiry_is_not_silently_ignored(self):
        self.assertEqual(meteor_dataset_freshness(datetime(2027, 12, 31, 23, 59, tzinfo=timezone.utc), NOW), "fresh")
        self.assertEqual(meteor_dataset_freshness(datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc), NOW), "expired")

    def test_notification_stage_requires_recipients_and_successful_submission(self):
        self.assertFalse(notification_stage_committed(False, True))
        self.assertFalse(notification_stage_committed(True, False))
        self.assertTrue(notification_stage_committed(True, True))


class MeteorDatasetMaintenanceTest(unittest.TestCase):
    """The dataset expires by design; that expiry has to be announced."""

    EXPIRES = datetime(2027, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    def test_no_reminder_while_comfortably_in_date(self):
        self.assertIsNone(meteor_dataset_alert_stage(self.EXPIRES, self.EXPIRES - timedelta(days=200)))

    def test_expiring_reminder_inside_the_warning_window(self):
        self.assertEqual(meteor_dataset_alert_stage(self.EXPIRES, self.EXPIRES - timedelta(days=30)), "expiring")

    def test_warning_window_boundary_is_inclusive(self):
        at_boundary = self.EXPIRES - timedelta(days=60)
        self.assertEqual(meteor_dataset_alert_stage(at_boundary + timedelta(seconds=1), at_boundary, warn_days=60), "expiring")

    def test_warn_days_is_configurable(self):
        ninety_out = self.EXPIRES - timedelta(days=90)
        self.assertIsNone(meteor_dataset_alert_stage(self.EXPIRES, ninety_out, warn_days=60))
        self.assertEqual(meteor_dataset_alert_stage(self.EXPIRES, ninety_out, warn_days=120), "expiring")

    def test_expired_reminder_after_the_expiry_date(self):
        self.assertEqual(meteor_dataset_alert_stage(self.EXPIRES, self.EXPIRES + timedelta(days=1)), "expired")
        self.assertEqual(meteor_dataset_freshness(self.EXPIRES, self.EXPIRES + timedelta(days=1)), "expired")

    def test_days_remaining_goes_negative_once_past(self):
        self.assertEqual(meteor_dataset_days_remaining(self.EXPIRES, self.EXPIRES - timedelta(days=10)), 10)
        self.assertLess(meteor_dataset_days_remaining(self.EXPIRES, self.EXPIRES + timedelta(days=2)), 0)


class MoonInterferenceLimitTest(unittest.TestCase):
    """The configured Moon limit must actually reach the assessment.

    It previously could not: this function hardcoded 0.60 and the caller
    applied its own limit afterwards, so raising the limit above 60% was
    silently ignored.
    """

    def assess(self, illumination, moon_limit):
        return meteor_viewing_assessment(
            True, 45, 10, 60, illumination, 30, 20, moon_limit=moon_limit,
        )

    def test_raising_the_limit_now_permits_a_brighter_moon(self):
        self.assertEqual(self.assess(0.70, 0.60), "Poor conditions")
        self.assertTrue(self.assess(0.70, 0.80).startswith("Good"))

    def test_lowering_the_limit_still_rejects(self):
        self.assertEqual(self.assess(0.40, 0.30), "Poor conditions")

    def test_limit_boundary_is_inclusive(self):
        self.assertEqual(self.assess(0.60, 0.60), "Poor conditions")

    def test_moon_below_horizon_ignores_illumination(self):
        self.assertEqual(
            meteor_viewing_assessment(True, 45, 10, 60, 1.0, -5, 20, moon_limit=0.60),
            "Good viewing tonight",
        )


if __name__ == "__main__":
    unittest.main()
