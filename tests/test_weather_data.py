"""
Covers the CLI-prefix station-code bug: rules_extractor's station_code
comes back in Kalshi's own settlement-source format ("CLI" + 3-letter
city code, e.g. "CLIHOU"), not the real NWS/ICAO code weather.gov and
STATION_REFERENCE need ("KHOU"). Every case here was confirmed against
live weather.gov 404s in one real session, not guessed.
"""
from __future__ import annotations

import unittest

from weather_data import kalshi_station_to_nws_id, looks_like_us_station, STATION_REFERENCE


class TestKalshiStationToNwsId(unittest.TestCase):
    # Every one of these was directly observed 404ing against weather.gov
    # before the translation existed.
    CONFIRMED_REAL_CASES = {
        "CLIHOU": "KHOU", "CLIAUS": "KAUS", "CLIORD": "KORD", "CLIPHX": "KPHX",
        "CLISEA": "KSEA", "CLISAN": "KSAN", "CLISAT": "KSAT", "CLIDCA": "KDCA",
        "CLIDFW": "KDFW", "CLIBOS": "KBOS", "CLIATL": "KATL", "CLISFO": "KSFO",
        "CLIMSY": "KMSY", "CLIOKC": "KOKC",
    }

    def test_all_confirmed_real_cases_translate_correctly(self):
        for kalshi_code, expected_nws in self.CONFIRMED_REAL_CASES.items():
            with self.subTest(kalshi_code=kalshi_code):
                self.assertEqual(kalshi_station_to_nws_id(kalshi_code), expected_nws)

    def test_every_translated_code_has_station_reference_coverage(self):
        """The whole point of translating is to actually be USABLE — each
        of these needs a STATION_REFERENCE entry too, or forecast lookups
        still silently fail even after the code itself resolves."""
        for expected_nws in self.CONFIRMED_REAL_CASES.values():
            with self.subTest(nws_code=expected_nws):
                self.assertIn(expected_nws, STATION_REFERENCE)

    def test_already_correct_k_format_passes_through_unchanged(self):
        self.assertEqual(kalshi_station_to_nws_id("KAUS"), "KAUS")
        self.assertEqual(kalshi_station_to_nws_id("KHOU"), "KHOU")

    def test_edge_cases_fail_safe(self):
        self.assertIsNone(kalshi_station_to_nws_id(None))
        self.assertEqual(kalshi_station_to_nws_id(""), "")
        self.assertEqual(kalshi_station_to_nws_id("CLI"), "CLI")  # too short to match
        self.assertEqual(kalshi_station_to_nws_id("SOMETHING_ELSE"), "SOMETHING_ELSE")


class TestLooksLikeUsStation(unittest.TestCase):
    # Every international city actually seen in Kalshi's discovered
    # temperature series in one real session — weather.gov cannot ever
    # serve these regardless of station-code translation (it's a US-only
    # NWS system), so these must be rejected to avoid guaranteed 404s.
    CONFIRMED_INTERNATIONAL_CODES = [
        "RJTT", "RKSI", "LFPG", "LSGG", "EDDF", "EGLL", "EHAM",
        "EBBR", "MMMX", "CYYZ", "VABB", "VHHH", "WSSS", "YSSY", "ZBAA", "ZSPD",
    ]
    CONFIRMED_US_CODES = [
        "KHOU", "KAUS", "KORD", "KPHX", "KSEA", "KSAN", "KSAT", "KDCA",
        "KDFW", "KBOS", "KATL", "KSFO", "KMSY", "KOKC", "KLAS", "KMDW", "KDEN",
    ]

    def test_all_confirmed_international_codes_rejected(self):
        for code in self.CONFIRMED_INTERNATIONAL_CODES:
            with self.subTest(code=code):
                self.assertFalse(looks_like_us_station(code))

    def test_all_confirmed_us_codes_accepted(self):
        for code in self.CONFIRMED_US_CODES:
            with self.subTest(code=code):
                self.assertTrue(looks_like_us_station(code))

    def test_edge_cases_fail_safe(self):
        self.assertFalse(looks_like_us_station(None))
        self.assertFalse(looks_like_us_station(""))
        self.assertFalse(looks_like_us_station("K"))         # too short
        self.assertFalse(looks_like_us_station("KABCDE"))    # too long
        self.assertFalse(looks_like_us_station("khou"))      # lowercase — real codes are uppercase

    def test_end_to_end_translation_then_us_check(self):
        translated = kalshi_station_to_nws_id("CLIHOU")
        self.assertTrue(looks_like_us_station(translated))


if __name__ == "__main__":
    unittest.main()
