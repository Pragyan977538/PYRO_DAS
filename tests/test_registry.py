"""Stage 3: the registry.

Unit tests pin each rule on hand-made inputs; the fixture tests build a whole
registry in memory from the synthetic world, where the truth is known; the
database tests write it, rebuild it, and check that ids and provisional flags
survive. Database tests run on a throwaway ``firewatch_test_registry``.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest
from conftest import fresh_database, postgres_reachable

from firewatch.grid import CELL_M, cell_key, offset, to_metres
from firewatch.ingest.fixture import FOREST_BOX, PADDY_BOX, SOURCES, generate
from firewatch.ingest.normalize import dedupe_sp_nrt, normalise_firms
from firewatch.registry import evaluate as ev
from firewatch.registry.baseline import MIN_N, SOURCE_WIDE, baselines, lookup, season
from firewatch.registry.build import build, clear_from_rows, match_ids
from firewatch.registry.cells import Gate, add_metres, gated_cells, recurrence
from firewatch.registry.cluster import ASSIGN_M, CAP_KM, assign, cluster_cells
from firewatch.registry.fingerprint import FEATURES, month_stats, persistence

#: The fixture spans two years, so its tests use the two-year gate.
FIXTURE_GATE = Gate(months=3, days=10, years=2)
LOCATION_WORDS = ("lat", "lon", "x", "y", "dist", "landcover", "state", "firms_type",
                  "type2", "osm", "gihs")


# ------------------------------------------------------------------- helpers

def _detections(lat: float, lon: float, days: list[pd.Timestamp], per_day: int = 1,
                jitter_m: float = 0.0, seed: int = 0, frp: float = 10.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(days) * per_day
    la, lo = offset(np.full(n, lat), np.full(n, lon), rng.normal(0, jitter_m, n) if jitter_m
                    else np.zeros(n), rng.normal(0, jitter_m, n) if jitter_m else np.zeros(n))
    t = pd.DatetimeIndex(np.repeat(pd.DatetimeIndex(days), per_day)).tz_localize("UTC")
    return pd.DataFrame({"acq_datetime": t + pd.Timedelta(hours=20), "latitude": la,
                         "longitude": lo, "daynight": "N", "frp": frp,
                         "instrument": "VIIRS", "bt4": 330.0, "bt5": 300.0})


def _days(year: int, months: list[int], per_month: int) -> list[pd.Timestamp]:
    return [pd.Timestamp(year, m, d + 1) for m in months for d in range(per_month)]


# -------------------------------------------------------------------- gate

def test_gate_needs_months_days_and_years():
    """3 months x 4 days in each of two years passes; one year, or two months, fails."""
    ok = _detections(22.0, 70.0, _days(2022, [1, 2, 3], 4) + _days(2023, [5, 6, 7], 4))
    one_year = _detections(23.0, 71.0, _days(2022, [1, 2, 3, 4, 5, 6], 4))
    two_months = _detections(24.0, 72.0, _days(2022, [1, 2], 6) + _days(2023, [1, 2], 6))
    det = add_metres(pd.concat([ok, one_year, two_months], ignore_index=True))
    kept = gated_cells(recurrence(det), Gate(months=3, days=10, years=2))
    assert set(kept.index) == set(cell_key(*to_metres([22.0], [70.0])))


def test_gate_counts_days_not_detections():
    """Thirty detections on one night are one day of recurrence, not thirty."""
    det = add_metres(_detections(22.0, 70.0, _days(2022, [1, 2, 3], 3) +
                                 _days(2023, [1, 2, 3], 3), per_day=30))
    assert gated_cells(recurrence(det), Gate(3, 10, 2)).empty


# ----------------------------------------------------------------- cluster

def _cells(points_m: list[tuple[float, float]], n_det: int = 50) -> pd.DataFrame:
    xy = np.array(points_m, dtype=float)
    return pd.DataFrame({"x": xy[:, 0], "y": xy[:, 1], "n_det": n_det},
                        index=pd.Index(cell_key(xy[:, 0], xy[:, 1]), name="cell"))


def test_cluster_separates_sources_and_caps_landscapes():
    near = [(3e6, 2e6), (3e6 + CELL_M, 2e6)]                     # one source, 2 cells
    far = [(3e6 + 5000, 2e6)]                                     # another, 5 km off
    chain = [(3.2e6 + i * 400, 2.1e6) for i in range(60)]        # 24 km chain
    cells, table = cluster_cells(_cells(near + far + chain))
    labels = cells["cluster"].to_numpy()
    assert labels[0] == labels[1] >= 0 and labels[2] >= 0 and labels[2] != labels[0]
    assert (labels[3:] == -1).all(), "a chain wider than the cap is not a source"
    assert table["capped"].sum() == 1 and table.loc[table["capped"], "width_km"].iat[0] > CAP_KM


def test_assign_uses_cells_and_instrument_radius():
    cells, _ = cluster_cells(_cells([(3e6, 2e6), (3e6 + CELL_M, 2e6)]))
    x = np.array([3e6 + CELL_M + 450, 3e6 - 600, 3e6 - 900, 3e6 - 1200])
    y = np.full(4, 2e6)
    viirs = assign(x, y, cells, ASSIGN_M["VIIRS"])
    modis = assign(x, y, cells, ASSIGN_M["MODIS"])
    assert viirs.tolist() == [0, -1, -1, -1]     # 450 m from the east cell: in
    assert modis.tolist() == [0, 0, 0, -1]


# ------------------------------------------------------------- fingerprint

def test_persistence_counts_observable_nights_only():
    clear = pd.Series(np.r_[np.ones(10), np.zeros(10)], index=np.arange(20))  # 10 clear, 10 cloudy
    assert persistence(np.arange(10), clear, 0, 19) == pytest.approx(1.0)
    assert persistence(np.arange(5), clear, 0, 19) == pytest.approx(0.5)


def test_persistence_never_exceeds_one():
    """A detection through 'cloud' means the site was observable that night."""
    clear = pd.Series(np.full(20, 0.1), index=np.arange(20))
    assert persistence(np.arange(20), clear, 0, 19) == pytest.approx(1.0)
    assert 0 < persistence(np.arange(3), clear, 0, 19) <= 1


def test_persistence_is_nan_without_cloud_data():
    assert math.isnan(persistence(np.arange(5), pd.Series(dtype=float), 0, 19))


def test_month_stats():
    days = np.arange(24)
    entropy, peak, monsoon = month_stats(days, np.repeat(np.arange(1, 13), 2))
    assert entropy == pytest.approx(1.0) and peak == pytest.approx(1 / 12)
    assert monsoon == pytest.approx(4 / 12)
    entropy, peak, _ = month_stats(days, np.full(24, 11))
    assert entropy == pytest.approx(0.0) and peak == 1.0


def test_features_carry_no_location():
    """Every label is built from location; a location feature would leak it."""
    for name in FEATURES:
        assert not any(w == part for part in name.split("_") for w in LOCATION_WORDS), name


# ----------------------------------------------------------------- baseline

def _frp_frame(values, dn="N", month=1, instrument="VIIRS", source=0) -> pd.DataFrame:
    return pd.DataFrame({"source": source, "instrument": instrument, "daynight": dn,
                         "acq_datetime": pd.Timestamp(2023, month, 1, tz="UTC"),
                         "frp": np.asarray(values, dtype=float)})


def test_baselines_bucket_and_fall_back():
    det = pd.concat([_frp_frame(np.full(40, 10.0), "N", 1),       # VIIRS|N|winter: 40
                     _frp_frame(np.full(20, 30.0), "N", 7),       # monsoon: only 20
                     _frp_frame(np.full(10, 50.0), "D", 7)])      # day: only 10
    b = baselines(det)[0]
    assert {"*", "VIIRS|N", "VIIRS|N|winter"} == set(b)
    assert lookup(b, "VIIRS", "N", 1)[0] == "VIIRS|N|winter"
    assert lookup(b, "VIIRS", "N", 7)[0] == "VIIRS|N"            # 20 < MIN_N
    assert lookup(b, "VIIRS", "D", 7)[0] == SOURCE_WIDE
    assert lookup(b, "MODIS", "N", 1)[0] == SOURCE_WIDE
    assert b["*"]["n"] == 70 and MIN_N == 30


def test_one_explosion_does_not_move_the_baseline():
    """The reason for median/MAD: sigma would absorb the blast, the median doesn't."""
    calm = np.random.default_rng(1).lognormal(np.log(10), 0.3, 500)
    before = baselines(_frp_frame(calm))[0]["*"]
    after = baselines(_frp_frame(np.r_[calm, 5000.0]))[0]["*"]
    assert after["med"] == pytest.approx(before["med"], rel=0.01)
    assert after["mad"] == pytest.approx(before["mad"], rel=0.02)
    assert np.std(np.r_[calm, 5000.0]) > 20 * np.std(calm)


def test_seasons_are_imd():
    assert [season(m) for m in (1, 3, 6, 10)] == ["winter", "pre_monsoon", "monsoon",
                                                  "post_monsoon"]


# ------------------------------------------------------------- id matching

def test_match_ids_is_one_to_one_by_overlap():
    new = pd.Series({1: 0, 2: 0, 3: 0, 4: 1, 5: 2}, name="cluster").rename_axis("cell")
    old = pd.Series({1: 7, 2: 7, 3: 9, 4: 9, 6: 11}, name="old").rename_axis("cell")
    assert match_ids(new, old) == {0: 7, 1: 9}                   # 2 is new


# ----------------------------------------------------- the fixture, in memory

def _fixture_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    fx = generate()
    frames = {}
    for inst in ("VIIRS", "MODIS"):
        parts = [normalise_firms(fx.firms[k]) for k in fx.firms
                 if ("modis" in k.lower()) == (inst == "MODIS")]
        frame = dedupe_sp_nrt(pd.concat(parts, ignore_index=True)).reset_index(drop=True)
        frame["detection_id"] = np.arange(len(frame)) + (0 if inst == "VIIRS" else 10**7)
        frames[inst] = frame
    return frames["VIIRS"], frames["MODIS"], fx.cloud


@pytest.fixture(scope="module")
def fixture_registry():
    viirs, modis, cloud = _fixture_frames()
    return build(viirs, modis, FIXTURE_GATE,
                 clear_reader=lambda s: clear_from_rows(s, cloud, 0.25))


def _nearest_km(reg, lat: float, lon: float) -> float:
    x, y = to_metres([lat], [lon])
    return float(np.hypot(reg.sources["x"] - x[0], reg.sources["y"] - y[0]).min() / 1000)


@pytest.mark.parametrize("spec", SOURCES, ids=lambda s: s.name)
def test_fixture_sources_registered_or_not(fixture_registry, spec):
    """Industry with history is registered; the new flare and the blowout are not."""
    km = _nearest_km(fixture_registry, spec.lat, spec.lon)
    if spec.expected == "registered":
        assert km < 1.0, f"{spec.name}: nearest source {km:.2f} km"
    else:
        assert km > 3.0, f"{spec.name} must stay out of the registry ({km:.2f} km)"


def test_fixture_landscapes_stay_on_road_a(fixture_registry):
    v = fixture_registry.viirs
    rx, ry = to_metres([30.20], [75.32])                           # the belt refinery
    for la0, la1, lo0, lo1 in (PADDY_BOX, FOREST_BOX):
        box = (v.latitude.between(la0, la1) & v.longitude.between(lo0, lo1)
               & (np.hypot(v.x - rx[0], v.y - ry[0]) > 1000))
        assert box.sum() > 1000
        assert (v.source[box] >= 0).mean() < 0.005


def test_fixture_fingerprints_and_baselines(fixture_registry):
    fp = fixture_registry.fingerprints
    assert set(FEATURES) <= set(fp.columns)
    assert ((fp["persistence_night"] > 0) & (fp["persistence_night"] <= 1)).all()
    assert (fp["temp_cov"] >= 0).all() and fp["width_km"].max() < CAP_KM
    assert set(fp.index) == set(fixture_registry.sources.index)
    for buckets in fixture_registry.baselines.values():
        assert SOURCE_WIDE in buckets and any(k.startswith("MODIS|") for k in buckets)


def test_fixture_evaluation_finds_the_refineries(fixture_registry):
    hits = ev.site_hits(fixture_registry.cells)
    assert hits["Reliance Jamnagar"]["found"] and hits["Nayara Vadinar"]["found"]
    assert not hits["HMEL Bathinda"]["found"]      # the fixture has no source there


# ----------------------------------------------------------------- database

needs_db = pytest.mark.skipif(not postgres_reachable(), reason="no PostgreSQL reachable")


@pytest.fixture(scope="module")
def registry_db(tmp_path_factory):
    from firewatch.ingest.firms_archive import load_archive
    from firewatch.ingest.observability import load_observability

    for data_dir in fresh_database("firewatch_test_registry", tmp_path_factory):
        load_archive([2022, 2023])
        load_observability([2022, 2023])
        yield data_dir


def _built():
    from firewatch.registry.build import read_detections
    return build(read_detections("VIIRS"), read_detections("MODIS"), FIXTURE_GATE)


@needs_db
def test_write_and_read_back(registry_db):
    from firewatch.db import fetch_all
    from firewatch.registry.build import write

    reg = _built()
    ids = write(reg)
    rows = fetch_all("SELECT source_id, n_detections, fingerprint, baselines, "
                     "ST_GeometryType(footprint) AS kind, ST_IsValid(footprint) AS ok "
                     "FROM sources ORDER BY source_id")
    assert len(rows) == len(reg.sources) == len(set(ids.values()))
    assert all(r["kind"] == "ST_Polygon" and r["ok"] for r in rows)
    assert all("*" in r["baselines"] for r in rows)
    assert all(0 < r["fingerprint"]["persistence_night"] <= 1 for r in rows)
    pointed = fetch_all("SELECT count(*) AS n FROM detections WHERE source_id IS NOT NULL")[0]
    assert pointed["n"] == sum(r["n_detections"] for r in rows)
    within = fetch_all("""
        SELECT count(*) AS far FROM detections d
         WHERE d.instrument = 'VIIRS' AND d.source_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM source_cells c WHERE c.source_id = d.source_id
                  AND ST_DWithin(c.geom::geography, d.geom::geography, 505))""")[0]
    assert within["far"] == 0, "every assigned VIIRS detection is within 500 m of its cells"


@needs_db
def test_rebuild_keeps_ids_and_provisional_flags(registry_db):
    from firewatch.db import fetch_all, get_conn
    from firewatch.registry.build import write

    before = {r["source_id"]: r["lon"] for r in fetch_all(
        "SELECT source_id, round(ST_X(geom)::numeric, 3) AS lon FROM sources")}
    keep = min(before)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE sources SET provisional = TRUE WHERE source_id = %s", (keep,))
        cur.execute("INSERT INTO sources (geom, provisional, baselines) VALUES "
                    "(ST_SetSRID(ST_MakePoint(80, 20), 4326), TRUE, %s) RETURNING source_id",
                    (json.dumps({"*": {"med": 1, "mad": 1, "p99": 1, "n": 1}}),))
        stray = cur.fetchone()[0]
        cur.execute("INSERT INTO sources (geom) VALUES (ST_SetSRID(ST_MakePoint(81, 21), 4326)) "
                    "RETURNING source_id")
        stale = cur.fetchone()[0]
    write(_built())
    after = {r["source_id"]: (r["lon"], r["provisional"]) for r in fetch_all(
        "SELECT source_id, round(ST_X(geom)::numeric, 3) AS lon, provisional FROM sources")}
    assert {sid: lon for sid, (lon, _) in after.items() if sid in before} == before
    assert after[keep][1] is True, "a rebuild never clears provisional"
    assert stray in after, "an unmatched provisional source is left for Stage 5"
    assert stale not in after, "an unmatched registry source is removed"
