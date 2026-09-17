"""
Foundation for retrospective backtesting, explicitly requested: "if we
go back, extract all of that available data that is applicable to the
trades." These tests verify parsing against Open-Meteo's documented
response shape via mocking — the real API itself could not be reached
from the environment that wrote this module (blocked by that
environment's own network allowlist, confirmed directly), so this is
the strongest verification available until it runs somewhere with real
network access.
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch, MagicMock

import historical_weather as hw


def _mock_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


class TestGetHistoricalForecastHourly(unittest.TestCase):
    def test_parses_a_realistic_response_correctly(self):
        payload = {
            "hourly": {
                "time": ["2024-07-15T00:00", "2024-07-15T01:00"],
                "temperature_2m": [30.0, 31.5],
                "precipitation": [0.0, 0.2],
                "precipitation_probability": [10, 15],
            }
        }
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = _mock_response(payload)
            points = hw.get_historical_forecast_hourly(30.1975, -97.6664, date(2024, 7, 15), date(2024, 7, 15))

        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[0].temperature_f, 86.0, places=1)
        self.assertEqual(points[0].precipitation_probability_pct, 10)
        self.assertEqual(points[1].precipitation_mm, 0.2)

    def test_correct_url_and_params_are_sent(self):
        payload = {"hourly": {"time": [], "temperature_2m": [], "precipitation": [], "precipitation_probability": []}}
        with patch("httpx.Client") as MockClient:
            mock_get = MockClient.return_value.__enter__.return_value.get
            mock_get.return_value = _mock_response(payload)
            hw.get_historical_forecast_hourly(30.1975, -97.6664, date(2024, 7, 15), date(2024, 7, 16),
                                                model="gfs_seamless")
            call_args = mock_get.call_args
        self.assertEqual(call_args[0][0], hw.HISTORICAL_FORECAST_BASE)
        self.assertEqual(call_args[1]["params"]["models"], "gfs_seamless")
        self.assertEqual(call_args[1]["params"]["start_date"], "2024-07-15")
        self.assertEqual(call_args[1]["params"]["end_date"], "2024-07-16")

    def test_unexpected_response_shape_raises_loudly(self):
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = _mock_response({"error": "bad request"})
            with self.assertRaises(ValueError):
                hw.get_historical_forecast_hourly(30.1975, -97.6664, date(2024, 7, 15), date(2024, 7, 15))


class TestGetHistoricalObservationHourly(unittest.TestCase):
    def test_parses_correctly_and_pop_stays_none(self):
        payload = {
            "hourly": {
                "time": ["2024-07-15T00:00"],
                "temperature_2m": [25.0],
                "precipitation": [0.0],
            }
        }
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = _mock_response(payload)
            points = hw.get_historical_observation_hourly(30.1975, -97.6664, date(2024, 7, 15), date(2024, 7, 15))
        self.assertIsNone(points[0].precipitation_probability_pct)
        self.assertAlmostEqual(points[0].temperature_f, 77.0, places=1)

    def test_no_model_parameter_is_sent_for_observations(self):
        """The observation archive isn't per-model -- it's the
        reconstructed ground truth, so no models= param should ever be sent."""
        payload = {"hourly": {"time": [], "temperature_2m": [], "precipitation": []}}
        with patch("httpx.Client") as MockClient:
            mock_get = MockClient.return_value.__enter__.return_value.get
            mock_get.return_value = _mock_response(payload)
            hw.get_historical_observation_hourly(30.1975, -97.6664, date(2024, 7, 15), date(2024, 7, 15))
            call_args = mock_get.call_args
        self.assertEqual(call_args[0][0], hw.HISTORICAL_OBSERVATION_BASE)
        self.assertNotIn("models", call_args[1]["params"])

    def test_unexpected_response_shape_raises_loudly(self):
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = _mock_response({"error": "bad request"})
            with self.assertRaises(ValueError):
                hw.get_historical_observation_hourly(30.1975, -97.6664, date(2024, 7, 15), date(2024, 7, 15))


class TestCelsiusConversion(unittest.TestCase):
    def test_converts_correctly(self):
        self.assertAlmostEqual(hw._celsius_to_f(0), 32.0)
        self.assertAlmostEqual(hw._celsius_to_f(100), 212.0)
        self.assertAlmostEqual(hw._celsius_to_f(30), 86.0)

    def test_none_stays_none(self):
        self.assertIsNone(hw._celsius_to_f(None))


if __name__ == "__main__":
    unittest.main()
