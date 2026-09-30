"""Stage 1 acceptance: the synthetic test fixture and the spike injector.

A fixture is only useful if it reproduces what real data taught us. The first
synthetic benchmark modelled stubble as point sources and so hid the one failure
that mattered most. Several tests below pin the real-data behaviour directly: raw
clustering chains the paddy belt, the recurrence gate drops it but keeps the
refinery inside it, a five-month blowout never qualifies as infrastructure, and
temperatures are missing for most detections.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from firewatch.grid import cell_375, era5_cell, era5_cell_centre, offset, to_metres
from firewatch.ingest.fixture import (
    MODIS_COLUMNS,
    PADDY_BOX,
    SOURCES,
    VIIRS_COLUMNS,
    Fixture,
    generate,
    inject_spikes,
    summarise,
)
from firewatch.ingest.normalize import COLUMNS, dedupe_sp_nrt, normalise_firms

REPO = Path(__file__).resolve().parent.parent
FULL_PERIOD = [s.name for s in SOURCES if s.active is None]


@pytest.fixture(scope="module")
def fx() -> Fixture:
    return generate()


@pytest.fixture(scope="module")
def det(fx: Fixture) -> pd.DataFrame:
    return fx.truth["detections"]


def _in_box(frame: pd.DataFrame, box) -> pd.Series:
    la0, la1, lo0, lo1 = box
    return frame["latitude"].between(la0, la1) & frame["longitude"].between(lo0, lo1)


def _near(frame: pd.DataFrame, lat: float, lon: float, metres: float) -> pd.Series:
    x, y = to_metres(frame["latitude"], frame["longitude"])
    sx, sy = to_metres(lat, lon)
    return pd.Series(np.hypot(x - sx, y - sy) < metres, index=frame.index)


def _gate(frame: pd.DataFrame, months: int = 3, days: int = 10, years: int = 2) -> set[int]:
    """The recurrence gate from docs/DESIGN.md, on 375 m cells: the cells kept."""
    t = frame["acq_datetime"]
    cells = pd.DataFrame({"cell": cell_375(frame["latitude"], frame["longitude"]),
                          "year": t.dt.year, "month": t.dt.month, "day": t.dt.date})
    per_year = cells.groupby(["cell", "year"]).agg(m=("month", "nunique"),
                                                   d=("day", "nunique"))
    ok = ((per_year["m"] >= months) & (per_year["d"] >= days)).groupby(level="cell").sum()
    return set(ok.index[ok >= years])


# --------------------------------------------------------------- determinism

def test_same_seed_same_world_and_seeds_differ():
    a = generate(date(2023, 1, 1), date(2023, 3, 31), seed=3)
    b = generate(date(2023, 1, 1), date(2023, 3, 31), seed=3)
    c = generate(date(2023, 1, 1), date(2023, 3, 31), seed=4)
    pd.testing.assert_frame_equal(a.truth["detections"], b.truth["detections"])
    for key in a.firms:
        pd.testing.assert_frame_equal(a.firms[key], b.firms[key])
    pd.testing.assert_frame_equal(a.vnf, b.vnf)
    assert not a.truth["detections"].equals(c.truth["detections"])


def test_rejects_an_empty_period():
    with pytest.raises(ValueError):
        generate(date(2023, 2, 1), date(2023, 1, 1))


# ------------------------------------------------------------ FIRMS shape

def test_archive_and_nrt_files_have_firms_headers(fx):
    for key, frame in fx.firms.items():
        expected = MODIS_COLUMNS if "modis" in key.lower() else VIIRS_COLUMNS
        if key.startswith("firms_nrt/"):
            expected = [c for c in expected if c != "type"]   # NRT carries no type
        assert list(frame.columns) == expected, key


REAL = REPO / "data" / "raw" / "firms" / "viirs-snpp_2023_India.csv"


@pytest.mark.skipif(not REAL.exists(), reason="real archive file not downloaded")
def test_fixture_header_matches_the_real_archive():
    assert list(pd.read_csv(REAL, nrows=0).columns) == VIIRS_COLUMNS


def test_both_instruments_and_both_products_normalise(fx):
    sensors = set()
    for instrument in ("VIIRS", "MODIS"):
        for product in ("SP", "NRT"):
            out = normalise_firms(fx.firms_frame(instrument, product))
            assert list(out.columns) == COLUMNS
            assert set(out["product"]) == {product}
            assert set(out["instrument"]) == {instrument}
            if product == "NRT":
                assert out["firms_type"].isna().all()
            else:
                assert set(out["firms_type"].dropna().unique()) <= {0, 2}
            sensors |= set(out["sensor"])
    assert sensors == {"VIIRS_SNPP", "VIIRS_NOAA20", "MODIS_AQUA", "MODIS_TERRA"}


def test_mixing_instruments_is_refused(fx):
    with pytest.raises(ValueError, match="can't be mixed"):
        fx.firms_frame()


def test_sp_nrt_overlap_is_real_and_dedupes(fx):
    frames = [normalise_firms(fx.firms_frame("VIIRS", p)) for p in ("SP", "NRT")]
    both = pd.concat(frames, ignore_index=True)
    day = both["sensor"] + "|" + both["acq_datetime"].dt.strftime("%Y-%m-%d")
    sp_days = set(day[both["product"] == "SP"])
    overlap = (both["product"] == "NRT") & day.isin(sp_days)
    assert overlap.sum() > 100, "fixture must exercise the SP/NRT overlap"
    out = dedupe_sp_nrt(both)
    out_day = out["sensor"] + "|" + out["acq_datetime"].dt.strftime("%Y-%m-%d")
    assert not ((out["product"] == "NRT") & out_day.isin(sp_days)).any()
    assert (out["product"] == "NRT").sum() > 0, "NRT beyond the SP end must survive"
    assert len(out) == len(both) - overlap.sum()


# ------------------------------------------------- measured real-data traits

def test_night_share_and_temperature_coverage_look_real(fx, det):
    s = summarise(fx)
    assert 0.25 <= s["night_share"] <= 0.40          # real India: 29-30%
    viirs_night = ((det["daynight"] == "N") & det["sensor"].isin(["N", "N20"])).sum()
    assert len(fx.vnf) < viirs_night                  # VNF: night-only, fits often fail
    assert s["vnf_share"] < s["night_share"]


def test_stubble_is_diffuse_and_recurs(det):
    paddy = det[det["origin_kind"] == "field"]
    t = paddy["acq_datetime"]
    cells = pd.DataFrame({"cell": cell_375(paddy["latitude"], paddy["longitude"]),
                          "year": t.dt.year, "month": t.dt.month})
    per_year = cells.groupby(["cell", "year"])["month"].nunique()
    assert per_year.median() <= 2, "a field burns in a season, not all year"
    years = cells.groupby("cell")["year"].nunique()
    assert (years >= 2).mean() > 0.3, "the same fields must burn again next year"


def test_raw_dbscan_chains_the_belt(det):
    """The failure the fixture exists to reproduce: raw clustering over two years
    merges the paddy belt into one giant 'source'."""
    from sklearn.cluster import DBSCAN

    belt = det[_in_box(det, PADDY_BOX)]
    x, y = to_metres(belt["latitude"], belt["longitude"])
    labels = DBSCAN(eps=500, min_samples=5, algorithm="ball_tree").fit_predict(np.c_[x, y])
    biggest = pd.Series(labels[labels >= 0]).value_counts()
    top = biggest.index[0]
    share = biggest.iloc[0] / len(belt)
    width_km = np.hypot(np.ptp(x[labels == top]), np.ptp(y[labels == top])) / 1000
    assert share > 0.5 and width_km > 10, (share, width_km)


def test_recurrence_gate_keeps_sources_and_drops_the_belt(det):
    kept = _gate(det)
    cell = cell_375(det["latitude"], det["longitude"])
    in_kept = pd.Series(np.isin(cell, list(kept)), index=det.index)

    for name in FULL_PERIOD:                      # every real source survives
        mine = det["origin_id"] == name
        assert in_kept[mine].mean() > 0.5, name

    refinery = next(s for s in SOURCES if s.name == "refinery_in_belt")
    fields = (det["origin_kind"] == "field") & ~_near(det, refinery.lat, refinery.lon, 1000)
    assert in_kept[fields].mean() < 0.02, "the paddy belt must not enter the registry"

    for name in ("blowout", "new_flare"):         # one season of burning is not infrastructure
        assert in_kept[det["origin_id"] == name].mean() < 0.05, name


def test_forest_fires_spread(det):
    forest = det[det["origin_kind"] == "forest"]
    x, y = to_metres(forest["latitude"], forest["longitude"])
    frame = pd.DataFrame({"fire": forest["origin_id"].to_numpy(), "x": x, "y": y,
                          "day": forest["acq_datetime"].dt.date.to_numpy()})
    fires = frame.groupby("fire").agg(days=("day", "nunique"),
                                      span=("x", lambda s: np.ptp(s)))
    long_fires = fires[fires["days"] >= 4]
    assert len(long_fires) > 10
    assert (long_fires["span"] > 1000).mean() > 0.7, "fires must move across pixels"


# ------------------------------------------------------------ scripted events

def test_scripted_events_are_what_the_truth_says(det, fx):
    events = fx.truth["events"].set_index("event_id")
    assert list(events["kind"]) == ["furnace_fire", "flare_blast", "warehouse_fire",
                                    "blowout", "new_flare"]

    e1 = det[det["origin_id"] == "E1"]
    assert len(e1) == 2 and set(e1["sensor"]) == {"N", "N20"}
    steel = next(s for s in SOURCES if s.name == "steel_works")
    assert np.allclose(e1["frp"], 8 * steel.frp_median)
    same_window = (det["origin_id"] == "steel_works") & (
        det["acq_datetime"].dt.date == e1["acq_datetime"].dt.date.iloc[0]) & (
        det["daynight"] == "D")
    assert not same_window.any(), "nothing may sit between the two spike passes"

    e2 = det[det["origin_id"] == "E2"]
    assert len(e2) == 1 and e2["frp"].iloc[0] == pytest.approx(25 * 12)

    blowout = det[det["origin_id"] == "blowout"]["acq_datetime"].dt.date
    assert blowout.min() >= events.loc["E4", "start"]
    assert blowout.max() <= events.loc["E4", "end"]
    assert (events.loc["E4", "end"] - events.loc["E4", "start"]).days >= 150


def test_facilities_cover_their_sources_and_the_warehouse_is_outside(fx):
    from shapely import wkt
    from shapely.geometry import Point

    facilities = fx.truth["facilities"].set_index("name")
    for name in FULL_PERIOD:
        spec = next(s for s in SOURCES if s.name == name)
        assert wkt.loads(facilities.loc[name, "wkt"]).contains(Point(spec.lon, spec.lat))

    from shapely.geometry import Polygon

    e3 = fx.truth["events"].set_index("event_id").loc["E3"]
    fence = wkt.loads(facilities.loc["power_plant", "wkt"])
    assert not fence.contains(Point(e3["longitude"], e3["latitude"]))
    lons, lats = zip(*fence.exterior.coords, strict=True)
    fx_m, fy_m = to_metres(lats, lons)                 # measure in metres, not degrees
    px, py = to_metres(e3["latitude"], e3["longitude"])
    gap = Polygon(zip(fx_m, fy_m, strict=True)).distance(Point(float(px), float(py)))
    assert gap == pytest.approx(400, abs=5), "the warehouse sits 400 m outside the fence"


# ------------------------------------------------------------- spike injector

@pytest.fixture(scope="module")
def history(det) -> pd.DataFrame:
    """A real-shaped source history: the steel works as VIIRS saw it."""
    h = det[(det["origin_id"] == "steel_works") & det["sensor"].isin(["N", "N20"])]
    return h.reset_index(drop=True)


def test_injected_spikes_round_trip(history):
    spiked, truth = inject_spikes(history, 10, passes=2, seed=1)
    assert len(truth) == 10
    touched = [label for rows in truth["rows"] for label in rows]
    assert len(touched) == len(set(touched)) == 20

    cut = int(np.ceil(len(history) * 0.5))
    first_eligible = history.sort_values("acq_datetime").iloc[cut]["acq_datetime"]
    baseline = history.sort_values("acq_datetime").iloc[:cut]["frp"].median()
    for _, ev in truth.iterrows():
        assert ev["baseline_median"] == pytest.approx(baseline)
        assert ev["start"] >= first_eligible
        expected = np.maximum(history.loc[ev["rows"], "frp"], baseline * ev["multiplier"])
        assert np.allclose(spiked.loc[ev["rows"], "frp"], expected)
        assert 6.0 <= ev["multiplier"] <= 12.0

    untouched = history.index.difference(touched)
    pd.testing.assert_frame_equal(spiked.loc[untouched], history.loc[untouched])


def test_injected_events_never_touch_each_other(history):
    _, truth = inject_spikes(history, 12, passes=2, seed=5)
    order = list(history.sort_values("acq_datetime").index)
    position = {label: i for i, label in enumerate(order)}
    spans = sorted((min(position[r] for r in rows), max(position[r] for r in rows))
                   for rows in truth["rows"])
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert start - end >= 2, "at least one untouched pass between events"


def test_injection_is_deterministic_and_supports_single_pass(history):
    a = inject_spikes(history, 5, passes=1, seed=9)
    b = inject_spikes(history, 5, passes=1, seed=9)
    pd.testing.assert_frame_equal(a[0], b[0])
    assert list(a[1]["rows"]) == list(b[1]["rows"])
    assert all(len(rows) == 1 for rows in a[1]["rows"])


def test_injection_refuses_what_it_cannot_do(history):
    with pytest.raises(ValueError, match="too short"):
        inject_spikes(history.head(10), 5)
    with pytest.raises(ValueError, match="unique"):
        inject_spikes(pd.concat([history, history]), 1)


# --------------------------------------------------------------- disk and CLI

def test_write_and_read_back(tmp_path):
    fx = generate(date(2023, 10, 1), date(2023, 12, 31), seed=2)
    written = fx.write(tmp_path)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    assert {"firms/viirs-snpp_2023_India.csv", "firms/modis_2023_India.csv",
            "firms_nrt/VIIRS_NOAA20_NRT.csv", "vnf/vnf.csv", "observability/cloud.csv",
            "truth/events.csv", "truth/sources.csv"} <= names
    raw = pd.read_csv(tmp_path / "firms/viirs-snpp_2023_India.csv",
                      dtype={"acq_time": str, "version": str})
    out = normalise_firms(raw)
    assert (out["sensor"] == "VIIRS_SNPP").all() and len(out) == len(raw)


def test_cli_writes_the_fixture(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "generate_fixture.py"), "--start",
         "2023-11-01", "--end", "2023-12-31", "--out", str(tmp_path)],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "firms" / "viirs-jpss1_2023_India.csv").exists()


def test_cloud_table_matches_the_observability_schema(fx):
    assert list(fx.cloud.columns) == ["cell_id", "obs_date", "daynight", "cloud_frac"]
    assert fx.cloud["cloud_frac"].between(0, 1).all()
    monsoon = pd.to_datetime(fx.cloud["obs_date"]).dt.month.isin([6, 7, 8, 9])
    assert fx.cloud.loc[monsoon, "cloud_frac"].mean() > fx.cloud.loc[~monsoon,
                                                                     "cloud_frac"].mean()


# -------------------------------------------------------------------- grid

def test_grid_helpers():
    lat, lon = 22.34, 69.87
    la, lo = offset(lat, lon, 1000.0, 0.0)
    x0, _ = to_metres(lat, lon)
    x1, _ = to_metres(la, lo)
    assert float(x1 - x0) == pytest.approx(1000.0, abs=1.0)
    c = era5_cell(lat, lon)
    clat, clon = era5_cell_centre(c)
    assert abs(float(clat) - lat) <= 0.125 and abs(float(clon) - lon) <= 0.125
    assert cell_375(lat, lon) == cell_375(lat + 1e-6, lon + 1e-6)
