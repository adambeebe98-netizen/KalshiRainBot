"""Why did the core backfill find zero events on dates that have games?

12K was written to the output directory and no events were recorded,
which is the shape of a scoreboard fetch that came back empty or a
completed-check that rejected everything. Both are cheap to tell apart
by printing what each step actually saw.

A concurrent job is hammering the same host, so a rate-limited response
swallowed by the retry loop is also a live possibility.
"""
import backfill_espn_core as core

SITE = core.SITE
day = "2026-01-18"

url = f"{SITE}/{core.LEAGUES['nfl']}/scoreboard"
print(f"GET {url}?dates={day.replace('-', '')}")

board = core._get(url, dates=day.replace("-", ""))
print(f"board is None: {board is None}")
if board:
    events = board.get("events", [])
    print(f"events: {len(events)}")
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        st = (comp.get("status") or {}).get("type", {})
        print(f"  {ev.get('id')} {ev.get('shortName'):<12} "
              f"completed={st.get('completed')} state={st.get('state')}")

    if events:
        eid = str(events[0]["id"])
        print(f"\nfetch_game({eid}):")
        got = core.fetch_game("nfl", eid)
        print(f"  plays={len(got['plays'])} probs={len(got['probabilities'])}")
        timed = [p for p in got["plays"] if p.get("wallclock")]
        print(f"  plays with wallclock: {len(timed)}/{len(got['plays'])}")
        if timed:
            print(f"  first timed: {timed[0]['wallclock']} "
                  f"Q{timed[0]['period']} {timed[0]['clock']}")
            print(f"  last  timed: {timed[-1]['wallclock']} "
                  f"Q{timed[-1]['period']} {timed[-1]['clock']}")
        if got["probabilities"]:
            pr = got["probabilities"][len(got["probabilities"]) // 2]
            print(f"  mid prob: home={pr['home_win_pct']} "
                  f"spread={pr['spread_cover_home']} over={pr['total_over']} "
                  f"play_id={pr['play_id']}")
            ids = {p["id"] for p in got["plays"]}
            hit = sum(1 for p in got["probabilities"]
                      if p["play_id"] in ids)
            print(f"  probs resolvable to a play: "
                  f"{hit}/{len(got['probabilities'])}")
