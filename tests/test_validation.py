"""Stage 10: the rules behind the headline number, and the demo path's shape.

The per-fire recall is only as honest as its window and its outcome ladder, both
fixed before any event was scored; these tests pin them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import demo  # noqa: E402
import validate_events as ve  # noqa: E402

from firewatch.inference import ROAD_A, ROAD_B, ROAD_C  # noqa: E402


def _row(start="2024-05-23", time="13:40", end=None):
    return pd.Series({"start": start, "start_time_ist": time, "end": end})


def test_window_with_a_published_time_is_24_hours_in_utc():
    start, end, known = ve.window(_row())
    assert known
    assert start == pd.Timestamp("2024-05-23 08:10", tz="UTC")      # 13:40 IST
    assert end - start == pd.Timedelta(hours=24)


def test_window_without_a_time_covers_the_day_and_the_next():
    start, end, known = ve.window(_row(time=float("nan")))
    assert not known
    assert start == pd.Timestamp("2024-05-22 18:30", tz="UTC")      # midnight IST
    assert end - start == pd.Timedelta(days=2)


def test_window_runs_to_a_published_end():
    start, end, _ = ve.window(_row(start="2020-06-09", time=float("nan"), end="2020-11-15"))
    assert end == pd.Timestamp("2020-11-15 18:30", tz="UTC")        # end of 15 Nov IST


def _det(road, category, reason="x"):
    return pd.DataFrame({"road": [road], "category": [category], "reason": [reason]})


@pytest.mark.parametrize(("road", "category", "expected"), [
    (ROAD_C, "forest", "flagged"),               # any alert counts, whatever its class
    (ROAD_A, "industrial", "flagged"),
    (ROAD_B, "industrial", "routine"),           # seen, but called the plant's normal
    (ROAD_A, "agricultural", "misclassified"),
    (ROAD_A, "unclassified", "misclassified"),
])
def test_outcome_ladder(road, category, expected):
    assert ve.outcome(_det(road, category)) == expected


def test_no_detection_is_no_signature():
    assert ve.outcome(_det(ROAD_A, "industrial").iloc[0:0]) == "no signature"


def test_one_flag_outranks_routine_detections():
    both = pd.concat([_det(ROAD_B, "industrial"), _det(ROAD_C, "industrial")])
    assert ve.outcome(both) == "flagged"


def test_verified_events_are_sourced_and_located_honestly():
    events = pd.read_csv(REPO / "reference" / "verified_events.csv",
                         dtype={"start_time_ist": str})
    assert "BAGHJAN-2020" in set(events["event_id"])
    assert events["event_id"].is_unique
    assert events["source"].str.startswith("http").all()
    located = events[events["published_lat"].notna()]
    assert (located["match_m"] >= 1000).all()
    # the location is from a publication or a map, never from the satellite record
    assert not located["location_basis"].str.contains("firms|viirs|modis", case=False).any()
    unlocated = events[events["published_lat"].isna()]
    assert (unlocated["location_basis"] == "not located").all()


def test_demo_stops_are_local_and_checked():
    assert 5 <= len(demo.STOPS) <= 8                       # three minutes at ~30 s a stop
    for stop in demo.STOPS:
        assert stop.url.startswith("/?") and "http" not in stop.url
        if "open=" in stop.url:
            kind = stop.url.split("open=")[1].split(":")[0]
            assert kind in ("event", "detection", "source")
            assert stop.api and stop.expect, stop.title       # a panel the check verifies
