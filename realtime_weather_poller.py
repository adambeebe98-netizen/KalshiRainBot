"""
Live weather observation capture -- runs continuously, forever, as its own
systemd service, separate from bot.py and realtime_kalshi_ws.py.

WHY POLLING, NOT A WEBSOCKET: real ASOS/AWOS weather stations only publish
official observations roughly once an hour, with occasional ~5-minute
"special" reports during rapidly-changing conditions -- confirmed via
NWS's own observation-frequency documentation. Nothing updates continuously
enough for a genuine push feed to mean anything here, so polling every
~2 minutes reliably catches every new observation as soon as it exists,
without needing (or being able to get) anything faster.

WHY THIS STARTS NOW ANYWAY: free/cheap sources of RAW, station-level METAR
data typically only keep a rolling ~30-day window, unlike Kalshi's own
market history, which stays available indefinitely. If this doesn't run
continuously starting now, that specific slice of ground-truth station
data for today is gone for good later -- the same "can't get it back"
concern driving the Kalshi WebSocket side, just for a different reason.

Reuses weather_data.get_station_latest_observation() directly -- the
existing, already-tested NWS integration -- rather than a second,
separate METAR-parsing path.
"""
from __future__ import annotations

import logging
import time

import storage
from weather_data import STATION_REFERENCE, get_station_latest_observation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("realtime_weather_poller")

POLL_INTERVAL_SECONDS = 120
BETWEEN_STATION_DELAY_SECONDS = 0.5  # spread requests out rather than firing
                                       # all ~29 at once each cycle -- a
                                       # small courtesy to a free, public,
                                       # no-key government API we intend to
                                       # keep hitting forever


def poll_once() -> tuple[int, int]:
    """One pass over every known station. Returns (new_count, error_count).
    Never raises -- a single station's failure (a station down, a
    transient NWS error) must never take down the other 28 for this cycle,
    let alone the whole continuously-running poller."""
    new_count = 0
    error_count = 0
    for station_code in STATION_REFERENCE:
        try:
            obs = get_station_latest_observation(station_code)
        except Exception:
            log.exception("Fetch failed for station %s", station_code)
            error_count += 1
            time.sleep(BETWEEN_STATION_DELAY_SECONDS)
            continue

        if obs is None or not obs.timestamp:
            # No observation available right now (station down, or NWS
            # simply hasn't published one at this station in a while) --
            # not an error, just nothing new to store this cycle.
            time.sleep(BETWEEN_STATION_DELAY_SECONDS)
            continue

        is_new = storage.save_realtime_weather_obs(
            station_code=station_code,
            ts=obs.timestamp,
            received_ts=int(time.time()),
            temp_f=obs.temperature_f,
            precip_last_hour_mm=obs.precipitation_last_hour_mm,
            precip_last_3hr_mm=obs.precipitation_last_3hr_mm,
            description=obs.description,
        )
        if is_new:
            new_count += 1
        time.sleep(BETWEEN_STATION_DELAY_SECONDS)

    return new_count, error_count


def run_poller() -> None:
    log.info("Starting weather poller for %d stations, every %ds",
              len(STATION_REFERENCE), POLL_INTERVAL_SECONDS)
    while True:
        cycle_start = time.time()
        try:
            new_count, error_count = poll_once()
            log.info("Poll cycle: %d new observations, %d station errors",
                      new_count, error_count)
        except Exception:
            # poll_once() already catches per-station errors; this is a
            # last-resort guard so the loop itself can never die.
            log.exception("Unexpected error in poll cycle")

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, POLL_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    storage.init_db()
    run_poller()
