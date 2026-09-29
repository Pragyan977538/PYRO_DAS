"""Stage 7: risk scoring.

Unit tests pin the geometry (disk and downwind-sector sums on a synthetic grid),
the arithmetic of H, E, V and the multiplicative score, and the asset typing; a
database test builds the register from the fixture's OSM and checks that
reseeding keeps hand-added rows.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from conftest import fresh_database, postgres_reachable

from firewatch.risk import assets as ra
from firewatch.risk import score as rs
from firewatch.risk.population import Population, disk_kernel, sector_kernel

# ------------------------------------------------------------------ kernels


def test_disk_kernel_area():
    k = disk_kernel(1.0, 1.0, 5.0)
    assert abs(k.sum() - np.pi * 25) / (np.pi * 25) < 0.1


@pytest.mark.parametrize(("bearing", "rows", "cols"), [(0, "north", None), (90, None, "east"),
                                                        (180, "south", None), (270, None, "west")])
def test_sector_kernel_points_the_right_way(bearing, rows, cols):
    k = sector_kernel(1.0, 1.0, 10.0, bearing)
    yy, xx = np.nonzero(k)
    cy, cx = k.shape[0] // 2, k.shape[1] // 2
    if rows == "north":
        assert (yy < cy).all()
    if rows == "south":
        assert (yy > cy).all()
    if cols == "east":
        assert (xx > cx).all()
    if cols == "west":
        assert (xx < cx).all()
    assert k[cy, cx] == 0                       # the fire's own cell is not downwind


@pytest.fixture(scope="module")
def grid(tmp_path_factory):
    """A 1 km-ish grid with 1,000 people in one cell at (22.0 N, 80.0 E)."""
    import rasterio
    from rasterio.transform import from_origin

    step = 1 / 120
    path = tmp_path_factory.mktemp("pop") / "pop.tif"
    data = np.zeros((600, 600), dtype=np.float32)
    data[300, 300] = 1000.0
    west, north = 80.0 - 300 * step, 22.0 + 300 * step
    with rasterio.open(path, "w", driver="GTiff", width=600, height=600, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-99999,
                       transform=from_origin(west, north, step, step)) as ds:
        ds.write(data, 1)
    return Population(path), step


def test_population_within_5km(grid):
    pop, step = grid
    here = 22.0 - 0.5 * step, 80.0 + 0.5 * step          # the populated cell's centre
    near = pop.within_5km([here[0] + 3 / 110.574], [here[1]])[0]
    far = pop.within_5km([here[0] + 7 / 110.574], [here[1]])[0]
    assert near == pytest.approx(1000, rel=0.01) and far == pytest.approx(0, abs=1)


def test_population_downwind(grid):
    """People 5 km east count only when the wind blows east."""
    pop, step = grid
    fire = 22.0 - 0.5 * step, 80.0 + 0.5 * step - 5 / (111.32 * np.cos(np.radians(22)))
    east = pop.downwind([fire[0]], [fire[1]], [90])[0]
    west = pop.downwind([fire[0]], [fire[1]], [270])[0]
    calm = pop.downwind([fire[0]], [fire[1]], [np.nan])[0]
    assert east == pytest.approx(1000, rel=0.01) and west == 0 and np.isnan(calm)


def test_rasterise_and_sum(grid):
    pop, step = grid
    g = pop.rasterise([22.0 - 0.5 * step], [80.0 + 0.5 * step], [0.95])
    assert g.sum() == pytest.approx(0.95)
    s = pop.disk_sum(g, 10.0)
    assert pop.sample(s, [22.05], [80.0])[0] == pytest.approx(0.95, rel=0.01)


# ---------------------------------------------------------------- the score

def test_growth():
    score, word = rs.growth_score([12, 8, 10, 5], [10, 10, 10, np.nan])
    assert score.tolist() == [1.0, 0.0, 0.5, 0.5]
    assert word.tolist() == ["growing", "shrinking", "flat", "seen once"]


def test_hazard_uses_ranks_and_pixels():
    ref = np.sort(np.arange(1, 101, dtype=float))
    h = rs.hazard([100.0, 1.0], [3, 1], [1.0, 0.0], ref)
    assert h["frp_percentile"].tolist() == [1.0, 0.01]
    assert h["pixels_score"][0] == pytest.approx(0.5)            # 3 pixels: the midpoint
    assert h["score"][0] == pytest.approx(0.5 + 0.15 + 0.2)


def test_exposure_without_wind_moves_its_weight():
    ref = np.sort(np.arange(0, 1000, dtype=float))
    with_wind = rs.exposure([999], [999], [0.0], ref, ref)["score"][0]
    no_wind = rs.exposure([999], [np.nan], [0.0], ref, ref)["score"][0]
    assert with_wind == pytest.approx(0.75) and no_wind == pytest.approx(0.75)


def test_vulnerability_decays_and_takes_the_higher():
    v = rs.vulnerability([0.95, 0.95, np.nan], [0.0, 2000.0, np.nan], [np.nan, 0.8, np.nan])
    assert v[0] == pytest.approx(0.95)
    assert v[1] == pytest.approx(0.8)                            # own class beats a far asset
    assert v[2] == rs.FLOOR


def test_a_huge_fire_in_the_empty_scores_low():
    """The argument for multiplication, in one line."""
    remote = rs.risk(1.0, rs.FLOOR, rs.FLOOR)
    at_refinery = rs.risk(1.0, 0.8, 0.95)
    additive = 100 * (0.40 * 1.0 + 0.35 * rs.FLOOR + 0.25 * rs.FLOOR)
    assert remote < 20 < additive and at_refinery > 85


def test_breakdown_has_raw_values():
    row = pd.DataFrame([{
        "risk": 42.0, "h": 0.8, "peak_frp": 120.0, "frp_percentile": 0.97, "pixels": 4,
        "growth_word": "growing", "e": 0.6, "pop5": 12345.6, "pop_5km_percentile": 0.7,
        "pop_downwind": 2345.0, "bearing": 271.0, "wind_date": "2024-03-01",
        "assets_10km": 3, "asset_weight": 1.3, "v": 0.9, "nearest_name": "X Refinery",
        "nearest_type": "oil_refinery", "nearest_m": 240.0, "own_class": np.nan}])
    b = json.loads(rs.breakdowns(row)[0])
    assert b["hazard"]["pixels"] == 4 and b["exposure"]["pop_5km"] == 12346
    assert b["vulnerability"]["nearest"] == "X Refinery" and b["vulnerability"]["own_class"] is None


# ------------------------------------------------------------------- assets

@pytest.mark.parametrize(("group", "tags", "kind"), [
    ("oil_gas", {"industrial": "refinery"}, "oil_refinery"),
    ("oil_gas", {"man_made": "petroleum_well"}, "oil_gas_field"),
    ("industrial_other", {"name": "Indane LPG Bottling Plant"}, "lng_lpg_terminal"),
    ("industrial_other", {"industrial": "chemical"}, "chemical_plant"),
    ("industrial_other", {"name": "Urea plant", "product": "fertilizer"}, "fertiliser_plant"),
    ("thermal_power", {}, "thermal_power_station"),
    ("mining", {}, "mine"),
    ("industrial_other", {}, "industrial_estate"),
])
def test_osm_typing(group, tags, kind):
    assert ra.osm_type(group, tags) == kind
    assert 0 < ra.CRITICALITY[kind] <= 1


def test_nuclear_is_top_criticality():
    assert ra.CRITICALITY[ra.GPPD_TYPE["Nuclear"]] == 1.0


needs_db = pytest.mark.skipif(not postgres_reachable(), reason="no PostgreSQL reachable")


@pytest.fixture(scope="module")
def risk_db(tmp_path_factory):
    from firewatch.ingest.osm import load_osm

    for data_dir in fresh_database("firewatch_test_risk", tmp_path_factory):
        load_osm()
        yield data_dir


@needs_db
def test_register_builds_and_keeps_manual_rows(risk_db):
    from firewatch.db import fetch_all, get_conn

    first = ra.build()
    assert first["from_osm"] == 8
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO critical_assets (name, geom, footprint, asset_type, "
                    "criticality, source_ref) VALUES ('Test Depot', "
                    "ST_SetSRID(ST_MakePoint(80, 20), 4326), ST_SetSRID(ST_MakePoint(80, 20), "
                    "4326), 'ammunition_depot', 1.0, 'manual:test')")
    again = ra.build()
    assert again["from_osm"] == first["from_osm"]
    rows = fetch_all("SELECT count(*) AS n, count(*) FILTER (WHERE source_ref = 'manual:test') "
                     "AS manual FROM critical_assets")[0]
    assert rows["manual"] == 1 and rows["n"] == first["from_osm"] + 1
