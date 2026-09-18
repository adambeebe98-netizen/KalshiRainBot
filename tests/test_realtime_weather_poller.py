from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.helpers import use_temp_db

use_temp_db()

import storage
from realtime_weather_poller import poll_once
from weather_data import StationObservation


def _fake_observation(station_id: str, ts: str = "2026-09-18T08:00:00Z"):
    return StationObservation(
        station_id=station_id,
        precipitation_last_hour_mm=0.0,
        precipitation_last_3hr_mm=0.0,
        temperature_f=88.0,
        description="Clear",
        timestamp=ts,
    )


class TestPollOnce(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM realtime_weather_obs")

    @patch("realtime_weather_poller.time.sleep")  # don't actually wait during tests
    @patch("realtime_weather_poller.get_station_latest_observation")
    def test_counts_new_observations(self, mock_get, mock_sleep):
        mock_get.side_effect = lambda station_code: _fake_observation(station_code)
        new_count, error_count = poll_once()
        self.assertGreater(new_count, 0)
        self.assertEqual(error_count, 0)

    @patch("realtime_weather_poller.time.sleep")
    @patch("realtime_weather_poller.get_station_latest_observation")
    def test_repeat_observation_across_cycles_is_not_double_counted(self, mock_get, mock_sleep):
        mock_get.side_effect = lambda station_code: _fake_observation(station_code)
        first_new, _ = poll_once()
        second_new, _ = poll_once()  # same underlying observation, second poll
        self.assertGreater(first_new, 0)
        self.assertEqual(second_new, 0)

    @patch("realtime_weather_poller.time.sleep")
    @patch("realtime_weather_poller.get_station_latest_observation")
    def test_one_station_failing_does_not_stop_the_others(self, mock_get, mock_sleep):
        def side_effect(station_code):
            if station_code == "KAUS":
                raise RuntimeError("simulated NWS outage for this station")
            return _fake_observation(station_code)

        mock_get.side_effect = side_effect
        new_count, error_count = poll_once()
        self.assertEqual(error_count, 1)
        self.assertGreater(new_count, 0)  # every other station still got recorded

    @patch("realtime_weather_poller.time.sleep")
    @patch("realtime_weather_poller.get_station_latest_observation")
    def test_station_with_no_observation_is_not_an_error(self, mock_get, mock_sleep):
        def side_effect(station_code):
            if station_code == "KAUS":
                return None
            return _fake_observation(station_code)

        mock_get.side_effect = side_effect
        new_count, error_count = poll_once()
        self.assertEqual(error_count, 0)


if __name__ == "__main__":
    unittest.main()
