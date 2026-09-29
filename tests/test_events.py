"""Stage 6: event assembly.

Hand-made detections pin each rule -- the 750 m link, the 72 h re-ignition window,
the 10 km cap, Road B making no events, an anomaly's 72 h run, a provisional
source's single lifelong event -- and the lifecycle. The database test writes a
fixture replay's events and checks a rerun replaces them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import shapely
from conftest import fresh_database, postgres_reachable

from firewatch.inference import ROAD_A, ROAD_B, ROAD_C
from firewatch.inference.events import MAX_EVENT_KM, Assembler, assemble

T0 = pd.Timestamp("2024-03-01 08:00", tz="UTC")
X0, Y0 = 3.0e6, 2.0e6


def _det(dx, hours, road=ROAD_A, source=np.nan, alert=None, cls="forest", cat="forest",
         dy=0.0, frp=10.0):
    dx, hours = np.atleast_1d(dx).astype(float), np.atleast_1d(hours).astype(float)
    n = max(len(dx), len(hours))
    dx, hours = np.broadcast_to(dx, n), np.broadcast_to(hours, n)
    return pd.DataFrame({
        "detection_id": np.arange(n), "x": X0 + dx, "y": Y0 + dy,
        "acq_datetime": [T0 + pd.Timedelta(hours=h) for h in hours],
        "road": road, "source_id": source, "alert": alert, "pred_class": cls,
        "category": cat, "frp": frp})


def _run(frames, provisional=None, promotions=None, cells=None, as_of=None):
    det = pd.concat(frames, ignore_index=True)
    det["detection_id"] = np.arange(len(det))
    promotions = promotions if promotions is not None else pd.DataFrame(
        columns=["source_id", "first_seen", "promoted_at"])
    cells = cells if cells is not None else pd.DataFrame(columns=["source_id", "x", "y"])
    as_of = as_of or det["acq_datetime"].max() + pd.Timedelta(hours=1)
    events, assigned = assemble(det, provisional or {}, promotions, cells, as_of)
    return events, det.merge(assigned, on="detection_id", how="left")


# ------------------------------------------------------------------ linking

def test_neighbours_in_a_pass_are_one_event_and_far_ones_are_not():
    events, det = _run([_det([0, 600, 1200], 0), _det(5000, 0)])
    assert len(events) == 2
    assert det.loc[:2, "event"].nunique() == 1


def test_re_ignition_within_72h_joins_and_after_it_does_not():
    events, det = _run([_det(0, 0), _det(300, 48), _det(300, 48 + 96)])
    assert det["event"].tolist()[0] == det["event"].tolist()[1] != det["event"].tolist()[2]
    assert len(events) == 2


def test_a_chain_of_fires_is_capped():
    """A front moving 700 m a day for 20 days: 750 m links, but no event > 10 km."""
    events, _ = _run([_det(i * 700.0, i * 24.0) for i in range(20)])
    assert len(events) >= 2
    for wkt in events["footprint"]:
        g = shapely.from_wkt(wkt)
        assert g.geom_type == "Polygon" and g.is_valid
    assert events["n_detections"].sum() == 20


def test_road_b_makes_no_event():
    events, det = _run([_det(0, 0, road=ROAD_B, source=7), _det(10000, 0)])
    assert len(events) == 1 and det.loc[0, "event"] != det.loc[0, "event"]   # NaN


def test_anomaly_runs_72h_then_a_new_event():
    alerts = [_det(0, h, road=ROAD_C, source=7, alert=a, cls="heavy_industry",
                   cat="industrial") for h, a in ((0, None), (12, "confirmed"), (40, "confirmed"),
                                                  (200, "provisional"))]
    events, _ = _run(alerts)
    assert events["kind"].tolist() == ["anomaly", "anomaly"]
    assert events["alert"].tolist() == ["confirmed", "provisional"]
    assert events["n_detections"].tolist() == [3, 1]


def test_a_provisional_source_is_one_event_for_life():
    """Pre-promotion Road A history, a 20-day gap, and a fire 600 m away later:
    one incident from its first detection."""
    promotions = pd.DataFrame({"source_id": [9], "first_seen": [T0.date()],
                               "promoted_at": [T0 + pd.Timedelta(days=10)]})
    cells = pd.DataFrame({"source_id": [9], "x": [X0], "y": [Y0]})
    frames = [_det(100, [0, 24, 48]),                                   # Road A, pre-promotion
              _det(0, [24 * 12, 24 * 13], road=ROAD_C, source=9, alert="new_source"),
              _det(0, 24 * 35, road=ROAD_C, source=9, alert="new_source"),   # after 20 days
              _det(600, 24 * 36)]                                          # Road A beside it
    events, det = _run(frames, provisional={9: None}, promotions=promotions, cells=cells)
    assert len(events) == 1 and events["kind"].iat[0] == "new_source"
    assert events["first_seen"].iat[0] == T0 and events["n_detections"].iat[0] == 7
    assert events["status"].iat[0] in ("active", "dormant")


# ---------------------------------------------------------------- lifecycle

@pytest.mark.parametrize(("hours_after", "status"), [(10, "active"), (50, "dormant"),
                                                     (100, "closed")])
def test_lifecycle(hours_after, status):
    events, _ = _run([_det(0, 0)], as_of=T0 + pd.Timedelta(hours=hours_after))
    assert events["status"].iat[0] == status


def test_retired_provisional_source_closes():
    frames = [_det(0, 0, road=ROAD_C, source=9, alert="new_source")]
    retired = T0 + pd.Timedelta(days=100)
    open_, _ = _run(frames, provisional={9: None}, as_of=T0 + pd.Timedelta(days=200))
    closed, _ = _run(frames, provisional={9: retired}, as_of=T0 + pd.Timedelta(days=200))
    assert open_["status"].iat[0] == "dormant" and closed["status"].iat[0] == "closed"


def test_reasons_and_classes():
    events, _ = _run([_det(0, [0, 1, 25], cls="agricultural", cat="agricultural")])
    row = events.iloc[0]
    assert row["event_class"] == "agricultural" and row["category"] == "agricultural"
    assert row["reason"] == "agricultural fire: 3 detections on 2 days, peak 10 MW"
    one, _ = _run([_det(0, 0, cls="agricultural", cat="agricultural")])
    assert one["reason"].iat[0] == "agricultural fire: 1 detection on 1 day, peak 10 MW"


def test_assembler_state_is_incremental():
    """Feeding days one at a time is what the live run does."""
    asm = Assembler()
    for h in (0, 24, 48):
        asm.add(_det(0, h))
    events, assigned = asm.result(T0 + pd.Timedelta(days=3))
    assert len(events) == 1 and len(assigned) == 3
    assert MAX_EVENT_KM == 10.0


# ----------------------------------------------------------------- database

needs_db = pytest.mark.skipif(not postgres_reachable(), reason="no PostgreSQL reachable")


@pytest.fixture(scope="module")
def events_db(tmp_path_factory):
    from firewatch.ingest.firms_archive import load_archive
    from firewatch.ingest.observability import load_observability
    from firewatch.ingest.osm import load_osm
    from firewatch.registry.build import build, read_detections, write
    from firewatch.registry.cells import Gate

    for data_dir in fresh_database("firewatch_test_events", tmp_path_factory):
        load_archive([2022, 2023])
        load_observability([2022, 2023])
        load_osm()
        write(build(read_detections("VIIRS"), read_detections("MODIS"), Gate(3, 10, 2)))
        yield data_dir


@needs_db
def test_events_write_and_rerun(events_db):
    import sys
    from pathlib import Path

    from firewatch.db import fetch_all
    from firewatch.inference import events as ev
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from run_inference import run

    run(pd.Timestamp("2023-06-01", tz="UTC"), pd.Timestamp("2023-09-01", tz="UTC"))
    for _ in range(2):
        det, provisional, promotions, cells, meta = ev.read_run()
        events, assigned = ev.assemble(det, provisional, promotions, cells,
                                       meta["window_end"])
        stats = ev.write(events, assigned, meta)
        rows = fetch_all("SELECT count(*) AS n, count(*) FILTER (WHERE kind = 'new_source') "
                         "AS new_source, count(*) FILTER (WHERE NOT ST_IsValid(footprint)) "
                         "AS invalid FROM events")[0]
        assert rows["n"] == stats["events"] > 0 and rows["invalid"] == 0
        assert rows["new_source"] >= 1                     # the fixture's blowout
    linked = fetch_all("""
        SELECT count(*) FILTER (WHERE road = 2 AND event_id IS NOT NULL) AS b_linked,
               count(*) FILTER (WHERE road <> 2 AND event_id IS NULL) AS unlinked
          FROM detections WHERE road IS NOT NULL""")[0]
    assert linked["b_linked"] == 0 and linked["unlinked"] == 0
