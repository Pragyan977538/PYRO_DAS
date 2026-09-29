"""Stage 5: routing, Road A, Road C and promotion.

Unit tests pin each rule on hand-made inputs. The fixture replay runs the whole
engine over the synthetic world's scripted events -- a furnace fire (confirmed),
a flare blast (provisional), a blowout (never Road B), a new flare (promoted) --
and a database test checks the writer and that a rerun replaces, not duplicates.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import shapely
from conftest import fresh_database, postgres_reachable

from firewatch.grid import to_metres
from firewatch.inference import ROAD_A, ROAD_B, ROAD_C
from firewatch.inference import anomaly as an
from firewatch.inference.engine import ClearSky, Engine, dry_id_allocator, summarise
from firewatch.inference.promotion import MAX_WIDTH_KM, Promoter
from firewatch.inference.road_a import Context, classify
from firewatch.inference.router import Router
from firewatch.registry.baseline import lookup, pass_table

T0 = pd.Timestamp("2024-03-01 08:00", tz="UTC")


# ------------------------------------------------------------------- passes

def test_passes_split_by_sensor_and_overpass():
    det = pd.DataFrame({
        "source": [1, 1, 1, 1, 2],
        "sensor": ["N", "N", "N20", "N", "N"],
        "instrument": "VIIRS", "daynight": "D",
        "acq_datetime": [T0, T0 + pd.Timedelta(minutes=1), T0, T0 + pd.Timedelta(hours=12), T0],
        "frp": [5.0, 9.0, 4.0, 3.0, 7.0]})
    p = pass_table(det).sort_values(["source", "sensor", "acq_datetime"])
    assert len(p) == 4                                 # N twice, N20 once, source 2 once
    first = p[(p["source"] == 1) & (p["sensor"] == "N")].iloc[0]
    assert first["frp"] == 9.0 and first["n_det"] == 2 and first["frp_sum"] == 14.0


# ------------------------------------------------------------------- Road C

BASE = {1: {"VIIRS|D|pre_monsoon": {"med": 10.0, "mad": 2.0, "p99": 20.0, "n": 50},
            "*": {"med": 10.0, "mad": 2.0, "p99": 20.0, "n": 80}}}


def _passes(frps, instrument="VIIRS", hours=None):
    hours = hours if hours is not None else range(len(frps))
    return pd.DataFrame({"source": 1, "instrument": instrument, "daynight": "D",
                         "acq_datetime": [T0 + pd.Timedelta(hours=12 * h) for h in hours],
                         "frp": np.asarray(frps, dtype=float)})


def test_breach_needs_both_conditions_and_confirmation_needs_two():
    s = an.tiers(an.score(_passes([11, 35, 12, 35, 36]), BASE), an.Thresholds(7, 3))
    # 35: z = 0.6745*25/2 = 8.4 > 3.5 and 35 > 1.5*20 -> breach; not > 3*20 -> not extreme
    assert s["breach"].tolist() == [False, True, False, True, True]
    assert s["alert"].tolist() == [None, None, None, None, "confirmed"]


def test_extreme_single_pass_is_provisional():
    s = an.tiers(an.score(_passes([11, 90, 11]), BASE), an.Thresholds(7, 3))
    assert s["alert"].tolist() == [None, "provisional", None]


def test_consecutive_has_no_clock():
    """Two breaching detection passes a week apart, nothing normal between."""
    s = an.tiers(an.score(_passes([35, 36], hours=[0, 14]), BASE), an.Thresholds(7, 3))
    assert s["alert"].tolist() == [None, "confirmed"]


def test_state_carries_across_batches():
    last: dict = {}
    an.tiers(an.score(_passes([35]), BASE), an.Thresholds(7, 3), last)
    second = an.tiers(an.score(_passes([36], hours=[1]), BASE), an.Thresholds(7, 3), last)
    assert second["alert"].tolist() == ["confirmed"]


def test_a_pass_is_judged_only_by_its_own_instrument():
    """MODIS here has only the source-wide pool, which is mostly VIIRS: not judged."""
    s = an.tiers(an.score(_passes([500.0], instrument="MODIS"), BASE), an.Thresholds(7, 3))
    assert s["baseline_key"].tolist() == [None] and s["alert"].tolist() == [None]
    assert lookup(BASE[1], "MODIS", "D", 4) == ("*", BASE[1]["*"])
    assert lookup(BASE[1], "MODIS", "D", 4, source_wide=False) == (None, None)


def test_an_unjudged_pass_does_not_break_a_chain():
    mixed = pd.concat([_passes([35], hours=[0]), _passes([5], instrument="MODIS", hours=[1]),
                       _passes([36], hours=[2])], ignore_index=True)
    s = an.tiers(an.score(mixed, BASE), an.Thresholds(7, 3))
    assert s.sort_values("acq_datetime")["alert"].tolist() == [None, None, "confirmed"]


def test_reasons_say_why():
    s = an.tiers(an.score(_passes([11, 35, 36, 90]), BASE), an.Thresholds(7, 3))
    words = [an.reason(r) for _, r in s.iterrows()]
    assert words[0].startswith("normal for this site")
    assert words[1].startswith("watch") and words[2].startswith("CONFIRMED")
    assert words[3].startswith("CONFIRMED") or words[3].startswith("PROVISIONAL")


# ------------------------------------------------------------------- Road A

def _context(zero_is_sea=True):
    fx, fy = to_metres([22.0], [70.0])
    mx, my = to_metres([23.0], [86.0])
    gx, gy = to_metres([12.5, 13.5], [79.0, 79.0])
    industry = pd.DataFrame({
        "group": ["oil_gas", "mining", "industrial_other", "industrial_other"],
        "name": ["Test Refinery", None, "Rice Mill", "Works"],
        "evidence": ["osm:w1", "osm:w2", "osm:w3", "osm:w4"],
        "geometry": [shapely.Point(fx[0], fy[0]).buffer(300), shapely.Point(mx[0], my[0]),
                     shapely.Point(gx[0], gy[0]).buffer(100),
                     shapely.Point(gx[1], gy[1]).buffer(100)]})

    def cover(lat, lon):
        lat = np.asarray(lat)
        return np.select([lat < 10, lat < 11, lat < 12, lat < 13, lat < 14, lat < 15],
                         [0, 80, 10, 40, 50, 30], default=60).astype(np.int16)
    return Context(industry, cover, zero_is_sea=zero_is_sea)


def _det(lat, lon, month=11):
    x, y = to_metres(np.atleast_1d(lat), np.atleast_1d(lon))
    return pd.DataFrame({"latitude": np.atleast_1d(lat), "longitude": np.atleast_1d(lon),
                         "x": x, "y": y,
                         "acq_datetime": pd.Timestamp(2024, month, 5, tz="UTC")})


@pytest.mark.parametrize(("lat", "lon", "cls", "category", "words"), [
    (22.0, 70.0, "oil_gas", "industrial", "inside 'Test Refinery'"),
    (23.01, 86.0, "mining", "industrial", "bare ground within 2 km of a mapped mine"),
    (9.5, 72.0, "offshore", "industrial", "open sea"),
    (10.5, 72.0, "unclassified", "unclassified", "on water"),
    (11.5, 78.0, "forest", "forest", "tree cover"),
    (12.5, 78.0, "agricultural", "agricultural", "kharif stubble-burning season"),
    (13.5, 78.0, "unclassified", "unclassified", "built-up land"),
    (14.5, 78.0, "vegetation", "other_natural", "shrub, grass or wetland"),
    (18.0, 78.0, "unclassified", "unclassified", "bare ground with no mapped mine"),
])
def test_road_a_rules(lat, lon, cls, category, words):
    out = classify(_det(lat, lon), _context())
    assert out["pred_class"].iat[0] == cls and out["category"].iat[0] == category
    assert words in out["reason"].iat[0]


def test_generic_industry_needs_to_be_on_it():
    """Beside a rice mill on cropland is a crop fire; inside it, or beside works on
    built-up land, is industrial."""
    from firewatch.grid import offset
    lat, lon = offset(12.5, 79.0, 250.0, 0.0)     # 150 m outside the mill, on cropland
    out = classify(_det(float(lat), float(lon)), _context())
    assert out["pred_class"].iat[0] == "agricultural"
    assert "from a mapped industrial area, but not on it" in out["reason"].iat[0]
    assert classify(_det(12.5, 79.0), _context())["pred_class"].iat[0] == "industrial"
    lat, lon = offset(13.5, 79.0, 250.0, 0.0)     # beside the works, on built-up land
    out = classify(_det(float(lat), float(lon)), _context())
    assert out["pred_class"].iat[0] == "industrial"


def test_fixture_zero_is_not_sea():
    out = classify(_det(9.5, 72.0), _context(zero_is_sea=False))
    assert out["pred_class"].iat[0] == "unclassified" and out["reason"].iat[0]


# ----------------------------------------------------------------- promotion

def _road_a(x, y, days, cls="forest"):
    from firewatch.grid import cell_key
    xs, ys = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    return pd.DataFrame({"cell": cell_key(xs, ys), "x": xs, "y": ys,
                         "day": np.asarray(days), "pred_class": cls})


def test_a_persistent_site_is_promoted_and_consumed():
    p = Promoter()
    days = np.arange(100, 125)
    p.add(_road_a(np.full(25, 3e6), np.full(25, 2e6), days, "oil_gas"))
    found = p.check(124)
    assert len(found) == 1 and found[0].cls == "oil_gas" and found[0].days == 25
    assert p.check(125) == []                       # its history is consumed


def test_fires_that_move_on_are_not_promoted():
    p = Promoter()
    days = np.arange(100, 140)
    p.add(_road_a(3e6 + days * 400.0, np.full(40, 2e6), days))       # a front moving 400 m/day
    assert p.check(139) == []


def test_cloud_makes_fewer_days_enough():
    """Seen on 10 of 11 observable days in a cloudy month: promoted; the calendar rule
    (20 days) would still be waiting."""
    p = Promoter()
    days = np.arange(100, 130, 3)                   # every third day, 10 days
    p.add(_road_a(np.full(10, 3e6), np.full(10, 2e6), days))
    sky = pd.Series(0.0, index=np.arange(60, 140))  # overcast...
    sky.loc[days] = 1.0                            # ...except when it was seen
    assert p.check(129) == []                       # no cloud record: needs 20 days
    assert len(p.check(129, clear=lambda x, y: sky)) == 1


def test_width_cap():
    p = Promoter()
    days = np.tile(np.arange(100, 125), 8)
    xs = np.repeat(3e6 + np.arange(8) * 450.0, 25)  # 8 cells in a 3.5 km line
    p.add(_road_a(xs, np.full(len(xs), 2e6), days))
    assert MAX_WIDTH_KM < 3.5 and p.check(124) == []


# ------------------------------------------------------------------- router

def test_router_radius_per_instrument():
    r = Router(pd.DataFrame({"source_id": [7], "x": [3e6], "y": [2e6]}))
    got = r.route(np.array([3e6 + 450, 3e6 + 900, 3e6 + 900]), np.full(3, 2e6),
                  np.array(["VIIRS", "VIIRS", "MODIS"]))
    assert got.tolist() == [7, -1, 7]
    r.add(9, pd.DataFrame({"x": [3e6 + 5000], "y": [2e6]}))
    assert 9 in r.provisional and r.route(np.array([3e6 + 5100]), np.array([2e6]),
                                          np.array(["VIIRS"])).tolist() == [9]
    r.remove(9)
    assert 9 not in r.provisional


# ------------------------------------------------------ the fixture, replayed

REPLAY = (pd.Timestamp("2023-06-01", tz="UTC"), pd.Timestamp("2023-10-15", tz="UTC"))


@pytest.fixture(scope="module")
def replay():
    from firewatch.ingest.fixture import generate
    from firewatch.ingest.landcover import CLASSES  # noqa: F401  (import check)
    from firewatch.ingest.normalize import dedupe_sp_nrt, normalise_firms
    from firewatch.ingest.osm import label_group
    from firewatch.registry.baseline import baselines
    from firewatch.registry.build import build, clear_from_rows
    from firewatch.registry.cells import Gate

    fx = generate()
    frames = {}
    for inst in ("VIIRS", "MODIS"):
        parts = [normalise_firms(fx.firms[k]) for k in fx.firms
                 if ("modis" in k.lower()) == (inst == "MODIS")]
        f = dedupe_sp_nrt(pd.concat(parts, ignore_index=True)).reset_index(drop=True)
        f["detection_id"] = np.arange(len(f)) + (0 if inst == "VIIRS" else 10**7)
        frames[inst] = f
    reg = build(frames["VIIRS"].copy(), frames["MODIS"].copy(), Gate(3, 10, 2),
                clear_reader=lambda s: clear_from_rows(s, fx.cloud, 0.25))
    cells = reg.cells[reg.cells["cluster"] >= 0].rename(columns={"cluster": "source_id"})
    hist = pd.concat([reg.viirs, reg.modis], ignore_index=True)
    base = baselines(pass_table(hist[hist["acq_datetime"] < REPLAY[0]]))
    rows = []
    for r in fx.truth["facilities"].itertuples():
        tags = dict(p.split("=", 1) for p in r.tag.split(";"))
        g = shapely.from_wkt(r.wkt)
        lon, lat = shapely.get_coordinates(g).T
        x, y = to_metres(lat, lon)
        geom = shapely.Point(x[0], y[0]) if g.geom_type == "Point" else \
            shapely.Polygon(np.column_stack([x, y]))
        rows.append({"group": label_group(tags), "name": r.name,
                     "evidence": f"osm:w{r.facility_id}", "geometry": geom})
    ctx = Context(pd.DataFrame(rows), lambda la, lo: np.zeros(len(la), dtype=np.int16),
                  zero_is_sea=False)
    meta = pd.DataFrame({"cls": "heavy_industry", "cls_conf": 0.9}, index=reg.sources.index)
    engine = Engine(Router(cells[["source_id", "x", "y"]]), base, meta, ctx,
                    an.Thresholds(7, 3), dry_id_allocator(),
                    clear=ClearSky(fx.cloud.assign(source="fixture")))
    det = pd.concat([frames["VIIRS"], frames["MODIS"]], ignore_index=True)
    det = det[(det["acq_datetime"] >= REPLAY[0]) & (det["acq_datetime"] < REPLAY[1])]
    result = engine.run(det)
    merged = result.detections.merge(det[["detection_id", "latitude", "longitude",
                                          "acq_datetime", "frp"]], on="detection_id")
    return fx, result, merged


def _at(merged, event, radius_m=1500):
    x, y = to_metres(merged["latitude"], merged["longitude"])
    ex, ey = to_metres([event.latitude], [event.longitude])
    day = merged["acq_datetime"].dt.date
    return merged[(np.hypot(x - ex[0], y - ey[0]) < radius_m)
                  & (day >= event.start) & (day <= event.end)]


def _event(fx, eid):
    return next(e for e in fx.truth["events"].itertuples() if e.event_id == eid)


def test_replay_furnace_fire_is_confirmed(replay):
    fx, _, merged = replay
    hit = _at(merged, _event(fx, "E1"))
    assert (hit["alert"] == "confirmed").sum() == 1
    assert set(hit.loc[hit["alert"] == "confirmed", "road"]) == {ROAD_C}


def test_replay_flare_blast_is_provisional(replay):
    fx, _, merged = replay
    assert (_at(merged, _event(fx, "E2"))["alert"] == "provisional").sum() == 1


def test_replay_blowout_is_never_normal_and_is_promoted(replay):
    fx, result, merged = replay
    blowout = _at(merged, _event(fx, "E4"))
    assert len(blowout) > 100
    assert not (blowout["road"] == ROAD_B).any()
    assert (blowout["alert"] == "new_source").mean() > 0.8
    assert len(result.promotions) >= 1


def test_replay_raises_no_other_alerts_and_explains_road_a(replay):
    _, result, merged = replay
    stats = summarise(result)
    assert stats["road_a_without_reason"] == 0
    assert stats["alerts"] == {"confirmed": 1, "provisional": 1}
    assert (merged.loc[merged["road"] == ROAD_A, "reason"].str.len() > 0).all()


def test_replay_events_one_per_incident(replay):
    """Stage 6 on the replay: the blowout is one incident from its first fire, and
    the furnace fire one confirmed anomaly."""
    from firewatch.inference.events import assemble
    from firewatch.registry.cells import add_metres

    fx, result, merged = replay
    det = add_metres(merged.copy())
    det["source_id"] = pd.to_numeric(det["source_id"], errors="coerce")
    det["road"] = det["road"].astype(int)
    promotions = pd.DataFrame([{"source_id": p["source_id"], "first_seen": p["first_day"],
                                "promoted_at": p["promoted_at"]} for p in result.promotions])
    cells = pd.concat([p["cells"].assign(source_id=p["source_id"])
                       for p in result.promotions], ignore_index=True)
    events, assigned = assemble(det, {p["source_id"]: None for p in result.promotions},
                                promotions, cells, REPLAY[1])
    det = det.merge(assigned, on="detection_id", how="left")
    blowout = _at(det, _event(fx, "E4"))
    assert blowout["event"].nunique() == 1 and blowout["event"].notna().all()
    ev = events.set_index("event").loc[int(blowout["event"].iat[0])]
    assert ev["kind"] == "new_source"
    assert ev["first_seen"].date() == blowout["acq_datetime"].min().date()
    furnace = _at(det, _event(fx, "E1"))
    fire = events.set_index("event").loc[furnace["event"].dropna().astype(int).unique()]
    assert (fire["kind"] == "anomaly").sum() == 1
    assert fire.loc[fire["kind"] == "anomaly", "alert"].tolist() == ["confirmed"]
    assert not det.loc[det["road"] == ROAD_B, "event"].notna().any()


# ----------------------------------------------------------------- database

needs_db = pytest.mark.skipif(not postgres_reachable(), reason="no PostgreSQL reachable")


@pytest.fixture(scope="module")
def inference_db(tmp_path_factory):
    from firewatch.ingest.firms_archive import load_archive
    from firewatch.ingest.observability import load_observability
    from firewatch.ingest.osm import load_osm
    from firewatch.registry.build import build, read_detections, write
    from firewatch.registry.cells import Gate

    for data_dir in fresh_database("firewatch_test_inference", tmp_path_factory):
        load_archive([2022, 2023])
        load_observability([2022, 2023])
        load_osm()
        write(build(read_detections("VIIRS"), read_detections("MODIS"), Gate(3, 10, 2)))
        yield data_dir


@needs_db
def test_run_writes_and_a_rerun_replaces(inference_db):
    import sys

    from firewatch.db import fetch_all
    from firewatch.registry.cells import Gate  # noqa: F401
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent
                           / "scripts"))
    from run_inference import run

    window = (pd.Timestamp("2023-06-01", tz="UTC"), pd.Timestamp("2023-08-01", tz="UTC"))
    for _ in range(2):
        _, stats = run(*window)
        promoted = fetch_all("SELECT count(*) AS n FROM sources WHERE origin = 'promotion'")[0]
        roads = fetch_all("SELECT count(*) AS n, count(road) AS routed, "
                          "count(*) FILTER (WHERE road = 1 AND coalesce(reason, '') = '') "
                          "AS unexplained FROM detections WHERE acq_datetime >= :s "
                          "AND acq_datetime < :e", {"s": window[0], "e": window[1]})[0]
        assert roads["n"] == roads["routed"] > 0 and roads["unexplained"] == 0
        assert promoted["n"] == stats["promotions"] >= 1       # replaced, not duplicated
    passes = fetch_all("SELECT count(*) AS n FROM source_passes")[0]
    assert passes["n"] == stats["passes"]
    early = Router.from_db(as_of=window[0])
    assert not early.provisional, "a promotion inside the window is the future at its start"
