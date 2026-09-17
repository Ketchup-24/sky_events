"""Focused tests for Hornby aurora forecast classification."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "sky_events"))

from aurora_model import (  # noqa: E402
    KpPoint, cloud_forecast_is_fresh, evaluate_outlook, notification_delivery_decision,
    notification_decision, parse_noaa_points,
)


UTC = timezone.utc
PACIFIC = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 9, 10, 3, tzinfo=UTC)


def points(kp: float, start: datetime = NOW) -> list[KpPoint]:
    return [KpPoint(start, kp, "predicted")]


def clouds(cover: float, at: datetime = NOW) -> list[tuple[datetime, float]]:
    return [(at, cover)]


def outlook(kp: float, cloud: float, **kwargs):
    return evaluate_outlook(
        points(kp), clouds(cloud), NOW,
        latitude=49.5325, longitude=-124.6764, timezone_name=PACIFIC,
        dark_check=kwargs.pop("dark_check", lambda _: True), **kwargs,
    )


class AuroraOutlookTest(unittest.TestCase):
    def test_kp_below_five_is_unlikely(self):
        self.assertEqual(outlook(4.67, 30)["state"], "Unlikely")

    def test_kp_five_is_possible(self):
        self.assertEqual(outlook(5, 30)["state"], "Possible")

    def test_kp_six_with_clear_skies_is_promising(self):
        self.assertEqual(outlook(6, 30)["state"], "Promising tonight")

    def test_kp_six_with_heavy_cloud_is_possible(self):
        result = outlook(6, 75)
        self.assertEqual(result["state"], "Possible")
        self.assertIn("limiting", result["explanation"])

    def test_live_alert_after_dark_is_look_outside_now(self):
        result = outlook(4, 20, live_alert=True, live_probability=42)
        self.assertEqual(result["state"], "Look outside now")
        self.assertTrue(result["current_dark"])

    def test_live_alert_in_daylight_is_not_a_live_notification_state(self):
        result = outlook(6, 20, live_alert=True, dark_check=lambda _: False)
        self.assertEqual(result["state"], "Unavailable")

    def test_utc_point_crossing_local_midnight_uses_prior_viewing_night(self):
        # 07:00 UTC is midnight PST on the previous local calendar day.
        now = datetime(2026, 1, 2, 6, tzinfo=UTC)
        result = evaluate_outlook(
            [KpPoint(datetime(2026, 1, 2, 7, tzinfo=UTC), 6, "predicted")],
            [(datetime(2026, 1, 2, 7, tzinfo=UTC), 20)], now,
            latitude=49.5325, longitude=-124.6764, timezone_name=PACIFIC,
            dark_check=lambda _: True,
        )
        self.assertEqual(result["night_key"], "2026-01-01")
        self.assertEqual(result["state"], "Promising tonight")

    def test_missing_cloud_data_is_unavailable(self):
        result = evaluate_outlook(
            points(6), [], NOW, latitude=49.5325, longitude=-124.6764,
            timezone_name=PACIFIC, dark_check=lambda _: True,
        )
        self.assertEqual(result["state"], "Unavailable")

    def test_stale_cloud_forecast_is_rejected_before_evaluation(self):
        self.assertFalse(cloud_forecast_is_fresh(NOW - timedelta(hours=3, seconds=1), NOW, 3))
        self.assertTrue(cloud_forecast_is_fresh(NOW - timedelta(hours=3), NOW, 3))
        self.assertFalse(cloud_forecast_is_fresh(None, NOW, 3))

    def test_malformed_noaa_data_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_noaa_points([{"time_tag": "not-a-time", "kp": "bad"}])

    def test_notification_deduplication_and_live_recovery_edges(self):
        saved, advance, live = notification_decision({}, "Promising tonight", "2026-09-09")
        self.assertTrue(advance)
        self.assertFalse(live)
        saved, advance, live = notification_decision(saved, "Promising tonight", "2026-09-09")
        self.assertFalse(advance)
        self.assertFalse(live)
        saved, advance, live = notification_decision(saved, "Look outside now", "2026-09-09")
        self.assertTrue(live)

    def test_disabled_recipients_do_not_consume_notification_edges(self):
        saved, advance, live = notification_delivery_decision({}, "Promising tonight", "2026-09-09", False)
        self.assertEqual(saved, {"live_active": False})
        self.assertFalse(advance)
        self.assertFalse(live)
        saved, advance, live = notification_delivery_decision(saved, "Promising tonight", "2026-09-09", True)
        self.assertTrue(advance)
        self.assertFalse(live)
        saved, advance, live = notification_decision(saved, "Possible", "2026-09-09")
        self.assertFalse(live)
        saved, advance, live = notification_decision(saved, "Look outside now", "2026-09-09")
        self.assertTrue(live)


if __name__ == "__main__":
    unittest.main()
