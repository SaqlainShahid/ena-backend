from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch
from django.test import Client

from .services import _daily_logs, _schedule, calculate_trip_plan


def make_legs(distance, duration):
    return [
        {"distance_miles": distance * 0.25, "duration_hours": duration * 0.25, "from": "Current", "to": "Pickup"},
        {"distance_miles": distance * 0.75, "duration_hours": duration * 0.75, "from": "Pickup", "to": "Dropoff"},
    ]


class SchedulerTests(TestCase):
    def test_health_endpoint(self):
        response = Client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_calculate_endpoint_requires_post(self):
        response = Client().get("/api/trips/calculate")
        self.assertEqual(response.status_code, 405)

    @patch("trips.views.search_locations")
    def test_location_search_endpoint(self, search):
        search.return_value = [{"lat": 41.88, "lon": -87.62, "label": "Chicago, Illinois, United States"}]
        response = Client().get("/api/locations/search?q=Chicago")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["label"], "Chicago, Illinois, United States")

    def test_daily_log_is_filled_to_24_hours(self):
        start = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc)
        segments, _ = _schedule(make_legs(650, 10), start, 0, "Green Bay", "Chicago", "Dallas")
        logs = _daily_logs(segments, start)
        self.assertGreaterEqual(len(logs), 1)
        for log in logs:
            total = sum((segment_hours(item) for item in log["segments"]), 0)
            self.assertAlmostEqual(total, 24, places=2)

    def test_long_trip_has_break_and_rest(self):
        start = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc)
        segments, _ = _schedule(make_legs(1800, 29), start, 0, "Green Bay", "Chicago", "Dallas")
        remarks = [item["remark"] for item in segments]
        self.assertTrue("30-minute rest break" in remarks or "Fueling (30-minute break)" in remarks)
        self.assertIn("10-hour sleeper berth", remarks)

    def test_fuel_stop_is_inserted_after_1000_miles(self):
        start = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc)
        segments, _ = _schedule(make_legs(2200, 35), start, 0, "Green Bay", "Chicago", "Dallas")
        self.assertGreaterEqual(sum(item["remark"].startswith("Fueling") for item in segments), 2)

    def test_pickup_is_between_route_legs(self):
        start = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc)
        segments, _ = _schedule(make_legs(650, 10), start, 0, "Green Bay", "Chicago", "Dallas")
        driving = [item for item in segments if item["status"] == "driving"]
        pickup = next(item for item in segments if item["remark"] == "Pickup")
        self.assertEqual(pickup["location"], "Chicago")
        self.assertEqual(pickup["start"], driving[0]["end"])

    @patch("trips.services._route")
    @patch("trips.services._geocode")
    def test_api_service_validates_cycle_hours(self, geocode, route):
        with self.assertRaises(ValueError):
            calculate_trip_plan({"current_location": "A", "pickup_location": "B", "dropoff_location": "C", "cycle_used_hours": 71})
        geocode.side_effect = lambda value: {"lat": 1, "lon": 1, "label": value}
        route.return_value = {
            "distance_miles": 10,
            "duration_hours": 1,
            "geometry": {"coordinates": []},
            "legs": make_legs(10, 1),
        }
        result = calculate_trip_plan({"current_location": "A", "pickup_location": "B", "dropoff_location": "C", "cycle_used_hours": 0})
        self.assertIn("daily_logs", result)

    def test_daily_miles_are_reported(self):
        start = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc)
        segments, _ = _schedule(make_legs(650, 10), start, 0, "Green Bay", "Chicago", "Dallas")
        self.assertGreater(_daily_logs(segments, start)[0]["total_driving_miles"], 0)


def segment_hours(segment):
    start = datetime.fromisoformat(segment["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(segment["end"].replace("Z", "+00:00"))
    return (end - start).total_seconds() / 3600
