"""Stage 1.5 - real-data spike: does raw clustering chain landscapes, and does
ball_tree save memory?

Pulls one year of FIRMS VIIRS for India (public, no key), normalises it with the
production code in firewatch/ingest/normalize.py, and clusters it three ways:

  A   the original plan: DBSCAN on every detection, eps=500 m, min_samples=5,
      ball_tree. A_kd is the same with kd_tree; A_snpp uses S-NPP only (half the
      data) to measure how memory scales.
  B   min_samples=1 on the same year: a conservative stand-in for ten years.
      Where the same fields burn every year, ten years make nearly every point a
      core point -- which is what min_samples=1 does -- and ten years add links,
      never remove them. So B understates ten-year chaining.
  C   the replacement: 375 m cells, a recurrence gate, DBSCAN on the surviving
      cells with sample_weight, and a footprint cap.

Each clustering runs in a fresh subprocess so its peak memory is measured alone.
No database. Writes reports/stage1_5/{results.json, tables.md, *.png}.

    python scripts/spike_cluster.py [--year 2023] [--skip-memory]
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.ingest.normalize import dedupe_sp_nrt, normalise_firms  # noqa: E402

ARCHIVE_URL = "https://firms.modaps.eosdis.nasa.gov/data/country/{s}/{y}/{s}_{y}_India.csv"
# GIHS (Ma et al. 2024, CC BY 4.0): imagery-verified industrial heat sources, 2012-2021.
GIHS_URL = ("https://zenodo.org/api/records/10570342/files/"
            "Global%20Remote%20Industrial%20Heat%20Sources%20DatasetV3.0_20240126.rar/content")
SENSORS = ("viirs-snpp", "viirs-jpss1")  # S-NPP and NOAA-20: all VIIRS in 2023

EPS_M = 500            # VIIRS pixels are 375 m
MIN_SAMPLES = 5
CELL_M = 375           # one VIIRS pixel
GATE_MONTHS = 6        # proposed: distinct months a cell burns in, within one year
GATE_DAYS = 10         # proposed: distinct days a cell burns on, within one year
CAP_KM = 20.0          # footprint cap: a wider cluster is a landscape, not a source
SOURCE_KM = 2.0        # reporting bins for cluster width
LANDSCAPE_KM = 10.0
MEMORY_LIMIT_GB = 16   # refuse to run raw DBSCAN if the neighbour lists would exceed this

REGIONS = {  # name: (lat_min, lat_max, lon_min, lon_max) -- approximate boxes
    "Jamnagar": (22.15, 22.55, 69.55, 70.15),
    "Punjab paddy belt": (29.9, 31.7, 74.0, 76.5),
    "Jharia coalfield": (23.65, 23.85, 86.25, 86.50),
}

OUT = REPO / "reports" / "stage1_5"

# Reference palette (dataviz skill): light surface, ink tokens, one blue ramp for
# the ordinal width bins, the first three categorical slots for the regions.
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
ROAD_A = "#c3c2b7"
WIDTH_COLORS = {"source": "#6da7ec", "mid": "#256abf", "landscape": "#0d366b"}
REGION_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]


# --------------------------------------------------------------------------- data

def to_metres(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Equirectangular metres, exactly as CLAUDE.md specifies for clustering."""
    return lon * 111320 * np.cos(np.radians(lat)), lat * 110540


def fetch(year: int, raw_dir: Path) -> list[Path]:
    import requests

    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in SENSORS:
        path = raw_dir / f"{s}_{year}_India.csv"
        if not path.exists():
            print(f">> downloading {path.name}")
            tmp = path.with_suffix(".part")
            with requests.get(ARCHIVE_URL.format(s=s, y=year), stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            tmp.replace(path)
        paths.append(path)
    return paths


def load(paths: list[Path]) -> pd.DataFrame:
    frames = [
        normalise_firms(pd.read_csv(p, dtype={"acq_time": str, "version": str}))
        for p in paths
    ]
    df = dedupe_sp_nrt(pd.concat(frames, ignore_index=True))
    df["x"], df["y"] = to_metres(df["latitude"].to_numpy(), df["longitude"].to_numpy())
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    df["day"] = (df["acq_datetime"].dt.floor("D") - epoch).dt.days.astype(np.int32)
    df["month"] = df["acq_datetime"].dt.month.astype(np.int8)
    df["night"] = (df["daynight"] == "N").to_numpy()
    return df


def region_mask(df: pd.DataFrame, box: tuple[float, float, float, float]) -> np.ndarray:
    la0, la1, lo0, lo1 = box
    return ((df["latitude"] >= la0) & (df["latitude"] <= la1)
            & (df["longitude"] >= lo0) & (df["longitude"] <= lo1)).to_numpy()


# --------------------------------------------------------------------- clustering

def raw_dbscan(xy: np.ndarray, min_samples: int, algorithm: str) -> np.ndarray:
    from sklearn.cluster import DBSCAN
    return DBSCAN(eps=EPS_M, min_samples=min_samples, algorithm=algorithm,
                  n_jobs=2).fit_predict(xy)


def cell_table(df: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """Aggregate detections into 375 m cells with their recurrence statistics.

    Returns the cell table (indexed by cell key) and each detection's cell key.
    """
    cx = np.floor(df["x"].to_numpy() / CELL_M).astype(np.int64)
    cy = np.floor(df["y"].to_numpy() / CELL_M).astype(np.int64)
    key = cx * 100_000 + cy
    frame = pd.DataFrame({
        "cell": key, "x": df["x"].to_numpy(), "y": df["y"].to_numpy(),
        "lat": df["latitude"].to_numpy(), "lon": df["longitude"].to_numpy(),
        "day": df["day"].to_numpy(), "month": df["month"].to_numpy(),
        "night_day": np.where(df["night"].to_numpy(), df["day"].to_numpy(), -1),
        "type2": (df["firms_type"] == 2).fillna(False).to_numpy(dtype=bool),
    })
    g = frame.groupby("cell")
    cells = g.agg(
        n_det=("x", "size"), x=("x", "mean"), y=("y", "mean"),
        lat=("lat", "mean"), lon=("lon", "mean"),
        n_days=("day", "nunique"), n_months=("month", "nunique"),
        n_nights=("night_day", "nunique"), type2_frac=("type2", "mean"),
    )
    # nunique counted the -1 placeholder for cells that also have day detections
    has_day = g["night_day"].min() < 0
    cells["n_nights"] = cells["n_nights"] - has_day.astype(int)
    return cells, key


def grid_cluster(cells: pd.DataFrame, months: int = GATE_MONTHS,
                 days: int = GATE_DAYS) -> pd.DataFrame:
    """Gate cells on one year's recurrence, then cluster the survivors."""
    gated = cells[(cells["n_months"] >= months) & (cells["n_days"] >= days)].copy()
    return cluster_cells(gated)


def cluster_cells(gated: pd.DataFrame) -> pd.DataFrame:
    """DBSCAN the gated cells with sample_weight, then apply the footprint cap.

    Returns the gated cells with a ``source`` label (-1 = not registered) and the
    width of the cluster each belongs to.
    """
    from sklearn.cluster import DBSCAN

    if gated.empty:
        gated["source"] = pd.Series(dtype=int)
        gated["width_km"] = pd.Series(dtype=float)
        return gated
    labels = DBSCAN(eps=EPS_M, min_samples=MIN_SAMPLES, algorithm="ball_tree").fit_predict(
        gated[["x", "y"]].to_numpy(), sample_weight=gated["n_det"].to_numpy())
    gated["source"] = labels
    extent = gated[labels >= 0].groupby("source").agg(
        x0=("x", "min"), x1=("x", "max"), y0=("y", "min"), y1=("y", "max"))
    width = np.hypot(extent.x1 - extent.x0 + CELL_M, extent.y1 - extent.y0 + CELL_M) / 1000
    gated["width_km"] = gated["source"].map(width)
    # Footprint cap: a cluster wider than a landscape threshold is not a source.
    gated.loc[gated["width_km"] > CAP_KM, "source"] = -1
    return gated


# ------------------------------------------------------------ memory, per process

def peak_memory_bytes() -> int:
    import psutil
    info = psutil.Process().memory_info()
    peak = getattr(info, "peak_wset", None)
    if peak is not None:
        return int(peak)
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if sys.platform == "darwin" else rss * 1024)


def child(run: str, npz: Path, labels_out: Path) -> None:
    """Run one clustering in this fresh process and print its cost as JSON."""
    import psutil

    data = np.load(npz)
    xy = np.c_[data["x"], data["y"]]
    if run == "A_snpp":
        xy = xy[data["sensor"] == 0]
    before = psutil.Process().memory_info().rss
    t0 = time.perf_counter()
    if run in ("A_ball", "A_snpp"):
        labels = raw_dbscan(xy, MIN_SAMPLES, "ball_tree")
    elif run == "A_kd":
        labels = raw_dbscan(xy, MIN_SAMPLES, "kd_tree")
    elif run == "B":
        labels = raw_dbscan(xy, 1, "ball_tree")
    elif run == "C":
        frame = pd.DataFrame({
            "x": data["x"], "y": data["y"], "latitude": data["lat"],
            "longitude": data["lon"], "day": data["day"], "month": data["month"],
            "night": data["night"], "firms_type": data["firms_type"],
        })
        cells, _ = cell_table(frame)
        labels = grid_cluster(cells)["source"].to_numpy()
    else:
        raise SystemExit(f"unknown run {run}")
    secs = time.perf_counter() - t0
    np.save(labels_out, labels)
    print(json.dumps({"run": run, "n_points": int(len(xy)), "secs": round(secs, 1),
                      "rss_before_mb": round(before / 2**20),
                      "peak_mb": round(peak_memory_bytes() / 2**20)}))


def run_child(run: str, npz: Path, work: Path) -> tuple[dict, np.ndarray]:
    labels_out = work / f"labels_{run}.npy"
    proc = subprocess.run(
        [sys.executable, __file__, "--child", run, "--npz", str(npz),
         "--labels", str(labels_out)],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"child {run} failed:\n{proc.stderr[-2000:]}")
    stats = json.loads(proc.stdout.strip().splitlines()[-1])
    stats["delta_mb"] = stats["peak_mb"] - stats["rss_before_mb"]
    print(f"   {run:7s} {stats['secs']:7.1f} s   peak +{stats['delta_mb']:,} MB")
    return stats, np.load(labels_out)


def neighbour_total(xy: np.ndarray) -> int:
    """Sum of 500 m neighbourhood sizes: what DBSCAN materialises, counted cheaply."""
    from scipy.spatial import cKDTree
    counts = cKDTree(xy).query_ball_point(xy, r=EPS_M, return_length=True, workers=-1)
    return int(np.asarray(counts, dtype=np.int64).sum())


# ---------------------------------------------------------------------- analysis

def cluster_widths(labels: np.ndarray, x: np.ndarray, y: np.ndarray) -> pd.DataFrame:
    m = labels >= 0
    t = pd.DataFrame({"c": labels[m], "x": x[m], "y": y[m]}).groupby("c").agg(
        n=("x", "size"), x0=("x", "min"), x1=("x", "max"),
        y0=("y", "min"), y1=("y", "max"), cx=("x", "mean"), cy=("y", "mean"))
    t["width_km"] = np.hypot(t.x1 - t.x0, t.y1 - t.y0) / 1000
    return t


def point_width(labels: np.ndarray, table: pd.DataFrame, min_size: int) -> np.ndarray:
    """Width of the source each detection belongs to; NaN where it is in none.

    A cluster smaller than ``min_size`` is not a source (it matters for B, where
    min_samples=1 makes every singleton a 'cluster').
    """
    ok = table[table["n"] >= min_size]["width_km"]
    return pd.Series(labels).map(ok).to_numpy(dtype=float)


def summarise(name: str, width: np.ndarray, masks: dict[str, np.ndarray],
              near: np.ndarray | None = None) -> dict:
    """Per-region shares: detections that skip Road A, and in landscape blobs."""
    in_source = ~np.isnan(width) if near is None else near
    out = {"run": name, "regions": {}}
    for region, m in masks.items():
        n = int(m.sum())
        out["regions"][region] = {
            "detections": n,
            "skip_road_a": round(float(in_source[m].mean()), 4) if n else None,
            "in_landscape": round(float((width[m] > LANDSCAPE_KM).mean()), 4) if n else None,
            "widest_km": round(float(np.nanmax(width[m])), 1)
            if n and not np.isnan(width[m]).all() else 0.0,
        }
    out["india_in_landscape"] = round(float((width > LANDSCAPE_KM).mean()), 4)
    out["india_skip_road_a"] = round(float(in_source.mean()), 4)
    return out


def near_sources(df: pd.DataFrame, gated: pd.DataFrame) -> np.ndarray:
    """True where a detection is within 500 m of a registered source cell -- the
    router's question, so this is exactly who would skip Road A."""
    from scipy.spatial import cKDTree
    reg = gated[gated["source"] >= 0]
    if reg.empty:
        return np.zeros(len(df), dtype=bool)
    dist, _ = cKDTree(reg[["x", "y"]].to_numpy()).query(
        df[["x", "y"]].to_numpy(), distance_upper_bound=EPS_M, workers=-1)
    return np.isfinite(dist)


def load_gihs(raw_dir: Path) -> dict | None:
    """GIHS India objects (imagery-verified industrial heat sources), in metres.

    Evaluation only -- never a label. Returns None when the archive is missing or
    can't be unpacked.
    """
    import geopandas as gpd
    import requests

    rar = next((raw_dir / "gihs").glob("*.rar"), None)
    if rar is None:
        rar = raw_dir / "gihs" / "GIHS_V3.0_20240126.rar"
        rar.parent.mkdir(parents=True, exist_ok=True)
        print(f">> downloading {rar.name}")
        try:
            resp = requests.get(GIHS_URL, timeout=120)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"   GIHS download failed ({exc}); skipping the GIHS comparison")
            return None
        rar.write_bytes(resp.content)
    dest = raw_dir / "gihs" / "extracted"
    dest.mkdir(parents=True, exist_ok=True)
    shp = next(dest.rglob("*.shp"), None)
    for tar in (r"C:\Windows\System32\tar.exe", "bsdtar", "unrar"):
        if shp is not None:
            break
        args = [tar, "x", "-o+", str(rar), str(dest)] if tar == "unrar" else \
            [tar, "-xf", str(rar), "-C", str(dest)]
        try:
            subprocess.run(args, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        shp = next(dest.rglob("*.shp"), None)
    if shp is None:
        return None

    gihs = gpd.read_file(shp)
    if gihs.crs is None:
        gihs = gihs.set_crs(4326)  # the paper states WGS84
    cols = {c.lower(): c for c in gihs.columns}
    nation = cols.get("nation") or cols.get("nation_name")
    vtype = cols.get("type")  # 0 confirmed industrial, 1 not industrial, 2 uncertain
    india = gihs[gihs[nation].astype(str).str.contains("India", case=False)] if nation else gihs
    confirmed = india[india[vtype] == 0] if vtype else india
    # GIHS ends in 2021; objects still burning then are the fair target for 2023.
    active = confirmed[confirmed["date2021_p"] > 0] if "date2021_p" in confirmed else confirmed
    return {"file": shp.name, "india": int(len(india)), "confirmed": confirmed.to_crs(7755),
            "active": active.to_crs(7755)}


def _share_matched(left, right) -> float | None:
    """Share of ``left`` geometries with a ``right`` geometry within 1 km."""
    import geopandas as gpd
    if not len(left) or not len(right):
        return None
    hit = gpd.sjoin_nearest(left[["geometry"]], right[["geometry"]], how="left",
                            max_distance=1000, distance_col="d")
    return round(float(hit.groupby(level=0)["d"].min().notna().mean()), 3)


def gihs_match(gated: pd.DataFrame, gihs: dict) -> dict:
    """Recall of GIHS India objects by our sources, and the share of our sources
    that sit on a GIHS object (a lower bound on precision: GIHS stops in 2021 and
    does not try to be complete)."""
    import geopandas as gpd

    reg = gated[gated["source"] >= 0].groupby("source").agg(lat=("lat", "mean"),
                                                           lon=("lon", "mean"))
    sources = gpd.GeoDataFrame(reg, geometry=gpd.points_from_xy(reg["lon"], reg["lat"]),
                               crs="EPSG:4326").to_crs(7755)
    return {"recall_confirmed": _share_matched(gihs["confirmed"], sources),
            "recall_active_2021": _share_matched(gihs["active"], sources),
            "sources_on_gihs": _share_matched(sources, gihs["confirmed"]),
            "n_sources": int(len(sources))}


def absorption(labels: np.ndarray, width_table: pd.DataFrame,
               src_c: np.ndarray) -> pd.DataFrame:
    """For each gated source, the raw cluster that swallowed it.

    ``contamination`` is the share of that raw cluster's detections that are NOT
    persistent -- one-off fires. Above one half, the plant's baseline would be
    computed mostly from the crop or forest fires around it.
    """
    frame = pd.DataFrame({"c": src_c, "a": labels})
    mine = frame[(frame["c"] >= 0) & (frame["a"] >= 0)]
    if mine.empty:
        return pd.DataFrame(columns=["c", "a", "width_km", "contamination"])
    dominant = mine.groupby("c")["a"].agg(lambda s: s.mode().iat[0])
    size = frame[frame["a"] >= 0].groupby("a").size()
    persistent = mine.groupby("a").size()
    share = (persistent / size).reindex(dominant.to_numpy()).to_numpy()
    return pd.DataFrame({
        "c": dominant.index, "a": dominant.to_numpy(),
        "width_km": width_table["width_km"].reindex(dominant.to_numpy()).to_numpy(),
        "contamination": 1 - share,
    })


# ----------------------------------------------------------------------- figures

def _style(ax, title: str | None = None) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=7, length=0)
    if title:
        ax.set_title(title, loc="left", fontsize=9, color=INK, pad=6)


def width_bins(width: np.ndarray) -> dict[str, np.ndarray]:
    return {
        "none": np.isnan(width),
        "landscape": width > LANDSCAPE_KM,
        "mid": (width > SOURCE_KM) & (width <= LANDSCAPE_KM),
        "source": width <= SOURCE_KM,
    }


def figure_regions(df: pd.DataFrame, runs: dict[str, np.ndarray],
                   summaries: dict[str, dict], path: Path) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    titles = {
        "A": "A  plan as written\n1 year, min_samples=5",
        "B": "B  ten-year stand-in\nmin_samples=1",
        "C": "C  375 m cells + recurrence gate",
    }
    fig, axes = plt.subplots(3, 3, figsize=(13, 13), facecolor=SURFACE)
    for r, (region, box) in enumerate(REGIONS.items()):
        m = region_mask(df, box)
        lon, lat = df["longitude"].to_numpy()[m], df["latitude"].to_numpy()[m]
        for c, run in enumerate(("A", "B", "C")):
            ax = axes[r, c]
            w = runs[run][m]
            bins = width_bins(w)
            size = 2.0 if region.startswith("Punjab") else 5.0
            ax.scatter(lon[bins["none"]], lat[bins["none"]], s=size * 0.6, c=ROAD_A,
                       linewidths=0, rasterized=True)
            for key in ("landscape", "mid", "source"):
                sel = bins[key]
                ax.scatter(lon[sel], lat[sel], s=size, c=WIDTH_COLORS[key],
                           linewidths=0, rasterized=True)
            s = summaries[run]["regions"][region]
            label = (f"{s['skip_road_a']:.0%} skip Road A   "
                     f"{s['in_landscape']:.0%} in blobs >{LANDSCAPE_KM:g} km\n"
                     f"widest source {s['widest_km']:g} km")
            ax.text(0.01, 0.01, label, transform=ax.transAxes, fontsize=7.5,
                    color=INK2, va="bottom",
                    bbox={"facecolor": SURFACE, "edgecolor": "none", "alpha": 0.85,
                          "pad": 2})
            la0, la1, lo0, lo1 = box
            ax.set_xlim(lo0, lo1)
            ax.set_ylim(la0, la1)
            ax.set_aspect(1 / np.cos(np.radians((la0 + la1) / 2)))
            _style(ax, titles[run] if r == 0 else None)
            if c == 0:
                ax.set_ylabel(f"{region}\n{int(m.sum()):,} detections", fontsize=9,
                              color=INK)
    handles = [
        Line2D([], [], marker="o", ls="", color=WIDTH_COLORS["source"],
               label=f"source <= {SOURCE_KM:g} km wide"),
        Line2D([], [], marker="o", ls="", color=WIDTH_COLORS["mid"],
               label=f"{SOURCE_KM:g}-{LANDSCAPE_KM:g} km"),
        Line2D([], [], marker="o", ls="", color=WIDTH_COLORS["landscape"],
               label=f"> {LANDSCAPE_KM:g} km (landscape blob)"),
        Line2D([], [], marker="o", ls="", color=ROAD_A, label="no source: goes to Road A"),
    ]
    fig.legend(handles=handles, loc="upper center", ncol=4, frameon=False, fontsize=9,
               labelcolor=INK2, bbox_to_anchor=(0.5, 0.995))
    fig.suptitle("One year of VIIRS over India (2023): raw DBSCAN vs gated 375 m cells",
                 x=0.01, ha="left", y=1.02, fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.975))
    fig.savefig(path, dpi=130, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def figure_recurrence(cells: pd.DataFrame, path: Path) -> None:
    import matplotlib.pyplot as plt

    rng = np.random.default_rng(0)
    fig, ax = plt.subplots(figsize=(8, 5.5), facecolor=SURFACE)
    sample = cells.sample(min(len(cells), 60_000), random_state=0)
    ax.scatter(sample["n_months"] + rng.uniform(-0.3, 0.3, len(sample)), sample["n_days"],
               s=2, c=ROAD_A, linewidths=0, rasterized=True, label="all India (sample)")
    for color, (region, (la0, la1, lo0, lo1)) in zip(REGION_COLORS, REGIONS.items(),
                                                     strict=True):
        sel = cells[(cells.lat >= la0) & (cells.lat <= la1)
                    & (cells.lon >= lo0) & (cells.lon <= lo1)]
        ax.scatter(sel["n_months"] + rng.uniform(-0.3, 0.3, len(sel)), sel["n_days"],
                   s=9, c=color, linewidths=0, alpha=0.8, label=f"{region} ({len(sel):,} cells)")
    ax.axvline(GATE_MONTHS - 0.5, color=INK2, lw=1)
    ax.axhline(GATE_DAYS - 0.5, color=INK2, lw=1)
    ax.set_yscale("log")
    ax.text(GATE_MONTHS - 0.35, 0.97, f"passes the gate: >= {GATE_MONTHS} months "
            f"and >= {GATE_DAYS} days", transform=ax.get_xaxis_transform(),
            fontsize=8, color=INK2, va="top")
    ax.set_xticks(range(1, 13))
    ax.set_xlabel("distinct months with a detection (2023)", fontsize=9, color=INK2)
    ax.set_ylabel("distinct days with a detection (log)", fontsize=9, color=INK2)
    ax.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    _style(ax, "Recurrence per 375 m cell: industry burns all year, fields burn in one season")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)


def figure_memory(mem: dict[str, dict], path: Path) -> None:
    import matplotlib.pyplot as plt

    names = {
        "A_snpp": "A  raw, S-NPP only (1 sensor-year)",
        "A_ball": "A  raw, ball_tree (2 sensor-years)",
        "A_kd": "A  raw, kd_tree (2 sensor-years)",
        "B": "B  raw, min_samples=1",
        "C": "C  375 m cells + gate",
    }
    order = [k for k in names if k in mem]
    vals = [max(mem[k]["delta_mb"], 1) for k in order]
    fig, ax = plt.subplots(figsize=(8, 3.2), facecolor=SURFACE)
    ypos = np.arange(len(order))[::-1]
    ax.barh(ypos, vals, height=0.5, color="#2a78d6")
    for yv, v in zip(ypos, vals, strict=True):
        ax.text(v, yv, f"  {v:,} MB", va="center", fontsize=8, color=INK2)
    ax.set_yticks(ypos, [names[k] for k in order], fontsize=8, color=INK)
    ax.set_xlabel("peak extra memory for the clustering step (MB)", fontsize=8, color=INK2)
    ax.set_xlim(0, max(vals) * 1.25)
    ax.grid(True, axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    _style(ax, "Memory: the tree algorithm doesn't matter; the data size does")
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)


def figure_india(df: pd.DataFrame, gated: pd.DataFrame, path: Path) -> None:
    import matplotlib.pyplot as plt

    reg = gated[gated["source"] >= 0]
    src = reg.groupby("source").agg(lat=("lat", "mean"), lon=("lon", "mean"),
                                    n=("n_det", "sum"))
    fig, ax = plt.subplots(figsize=(8, 9), facecolor=SURFACE)
    ax.scatter(df["longitude"], df["latitude"], s=0.05, c=ROAD_A, linewidths=0,
               rasterized=True)
    ax.scatter(src["lon"], src["lat"], s=np.clip(np.sqrt(src["n"]) * 1.5, 6, 90),
               c="#2a78d6", edgecolors=SURFACE, linewidths=0.6, zorder=3)
    ax.set_aspect(1 / np.cos(np.radians(22)))
    ax.set_xlim(68, 98)
    ax.set_ylim(6, 37)
    _style(ax, f"{len(df):,} VIIRS detections in 2023 (grey) -> "
               f"{len(src):,} persistent sources (blue)")
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)


# -------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--year", type=int, default=2023)
    ap.add_argument("--skip-memory", action="store_true",
                    help="skip the subprocess memory runs; cluster in-process")
    ap.add_argument("--child")
    ap.add_argument("--npz", type=Path)
    ap.add_argument("--labels", type=Path)
    args = ap.parse_args()
    if args.child:
        child(args.child, args.npz, args.labels)
        return 0

    cfg = settings()
    raw_dir = cfg.raw_dir
    work = cfg.data_dir / "interim" / f"spike_{args.year}"
    work.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f">> loading {args.year} VIIRS for India")
    paths = fetch(args.year, raw_dir / "firms")
    df = load(paths)
    sensor_code = (df["sensor"] != "VIIRS_SNPP").to_numpy().astype(np.int8)
    npz = work / "points.npz"
    np.savez(npz, x=df["x"].to_numpy(), y=df["y"].to_numpy(), sensor=sensor_code,
             lat=df["latitude"].to_numpy(), lon=df["longitude"].to_numpy(),
             day=df["day"].to_numpy(), month=df["month"].to_numpy(),
             night=df["night"].to_numpy(),
             firms_type=df["firms_type"].fillna(-1).astype(int).to_numpy())

    results: dict = {"year": args.year, "files": {p.name: p.stat().st_size for p in paths}}
    results["data"] = {
        "detections": int(len(df)),
        "by_sensor": {k: int(v) for k, v in df["sensor"].value_counts().items()},
        "by_product": {k: int(v) for k, v in df["product"].value_counts().items()},
        "night_share": round(float(df["night"].mean()), 4),
        "firms_type": {str(k): int(v) for k, v in
                       df["firms_type"].value_counts(dropna=False).sort_index().items()},
    }
    print(f"   {len(df):,} detections, {results['data']['night_share']:.1%} at night")

    xy = df[["x", "y"]].to_numpy()
    snpp = sensor_code == 0
    from sklearn.neighbors import NearestNeighbors
    results["sklearn_auto_picks"] = NearestNeighbors(
        radius=EPS_M, algorithm="auto").fit(xy[:50_000])._fit_method
    t0 = time.perf_counter()
    nb_all, nb_snpp = neighbour_total(xy), neighbour_total(xy[snpp])
    results["neighbours"] = {
        "sum_2_sensor_years": nb_all, "sum_1_sensor_year": nb_snpp,
        "ratio": round(nb_all / nb_snpp, 2),
        "points_ratio": round(len(xy) / int(snpp.sum()), 2),
        "est_lists_gb_2_sensor_years": round(nb_all * 8 / 2**30, 2),
        "secs": round(time.perf_counter() - t0, 1),
    }
    print(f"   neighbour lists: {nb_all:,} entries "
          f"({results['neighbours']['est_lists_gb_2_sensor_years']} GB); "
          f"{results['neighbours']['ratio']}x for "
          f"{results['neighbours']['points_ratio']}x the points")

    # ---- the clusterings
    cells, _ = cell_table(df)
    if args.skip_memory:
        labels = {"A": raw_dbscan(xy, MIN_SAMPLES, "ball_tree"),
                  "B": raw_dbscan(xy, 1, "ball_tree")}
        mem = {}
    else:
        if nb_all * 8 / 2**30 > MEMORY_LIMIT_GB:
            raise SystemExit("raw DBSCAN would exceed the memory limit; that is the answer")
        print(">> clustering, one fresh process per run")
        mem, labels = {}, {}
        for run in ("A_snpp", "A_ball", "A_kd", "B", "C"):
            mem[run], lab = run_child(run, npz, work)
            labels[run] = lab
        labels["A"] = labels["A_ball"]
        same = bool(np.array_equal(labels["A_ball"], labels["A_kd"]))
        results["ball_vs_kd_identical_labels"] = same
    results["memory"] = mem

    x, y = df["x"].to_numpy(), df["y"].to_numpy()
    masks = {name: region_mask(df, box) for name, box in REGIONS.items()}
    tables = {run: cluster_widths(labels[run], x, y) for run in ("A", "B")}
    widths = {
        "A": point_width(labels["A"], tables["A"], MIN_SAMPLES),
        "B": point_width(labels["B"], tables["B"], MIN_SAMPLES),
    }
    gated = grid_cluster(cells)
    cell_of = np.floor(x / CELL_M).astype(np.int64) * 100_000 + np.floor(y / CELL_M).astype(np.int64)
    cell_width = gated.loc[gated["source"] >= 0, "width_km"]
    widths["C"] = pd.Series(cell_of).map(cell_width).to_numpy(dtype=float)
    near = near_sources(df, gated)

    summaries = {
        "A": summarise("A", widths["A"], masks),
        "B": summarise("B", widths["B"], masks),
        "C": summarise("C", widths["C"], masks, near=near),
    }
    results["summaries"] = summaries
    for run in ("A", "B"):
        t = tables[run][tables[run]["n"] >= MIN_SAMPLES]
        results[f"{run}_clusters"] = {
            "n": int(len(t)),
            "wider_than_landscape": int((t["width_km"] > LANDSCAPE_KM).sum()),
            "widest_km": round(float(t["width_km"].max()), 1),
        }

    # FIRMS type=2 (static land source) -- evaluation comparator only.
    t2 = (df["firms_type"] == 2).fillna(False).to_numpy(dtype=bool)
    results["firms_type2"] = {
        "detections": int(t2.sum()),
        "near_C_sources": round(float(near[t2].mean()), 3) if t2.any() else None,
        "C_source_detections_type2": round(float(t2[near].mean()), 3) if near.any() else None,
    }

    reg = gated[gated["source"] >= 0]
    src_c = pd.Series(cell_of).map(reg["source"]).fillna(-1).astype(int).to_numpy()
    per_source = pd.DataFrame({"s": src_c, "day": df["day"].to_numpy(),
                               "night": df["night"].to_numpy(),
                               "month": df["month"].to_numpy()})
    per_source = per_source[per_source["s"] >= 0]
    active = per_source.groupby("s").agg(days=("day", "nunique"),
                                         months=("month", "nunique"))
    active["nights"] = per_source[per_source["night"]].groupby("s")["day"].nunique()
    top = reg.groupby("source").agg(
        lat=("lat", "mean"), lon=("lon", "mean"), cells=("n_det", "size"),
        detections=("n_det", "sum"), width_km=("width_km", "first"),
        type2=("type2_frac", "mean"),
    ).join(active).fillna({"nights": 0}).sort_values("detections", ascending=False)
    results["C_sources"] = {
        "n": int(len(top)), "cells_gated": int(len(gated)), "cells_total": int(len(cells)),
        "widest_km": round(float(top["width_km"].max()), 1) if len(top) else 0.0,
        "top": top.head(15).round(3).reset_index().to_dict(orient="records"),
    }
    for name, (la0, la1, lo0, lo1) in REGIONS.items():
        inside = top[(top.lat >= la0) & (top.lat <= la1) & (top.lon >= lo0) & (top.lon <= lo1)]
        results["C_sources"][f"in {name}"] = inside.round(3).reset_index().to_dict(
            orient="records")

    # Did raw DBSCAN swallow real sources into clusters of one-off fires?
    results["absorption"] = {}
    for run in ("A", "B"):
        ab = absorption(labels[run], tables[run], src_c)
        swallowed = ab[ab["contamination"] > 0.5]
        results["absorption"][run] = {
            "sources_checked": int(len(ab)),
            "mostly_one_off_fires": int(len(swallowed)),
            "median_contamination": round(float(ab["contamination"].median()), 3)
            if len(ab) else None,
            "examples": swallowed.merge(top[["lat", "lon", "detections"]], left_on="c",
                                        right_index=True)
            .sort_values("detections", ascending=False).head(8).round(3)
            .to_dict(orient="records"),
        }

    gihs = load_gihs(raw_dir)
    if gihs is not None:
        results["gihs"] = {"file": gihs["file"], "india_objects": gihs["india"],
                           "india_confirmed": int(len(gihs["confirmed"])),
                           "india_confirmed_active_2021": int(len(gihs["active"])),
                           **gihs_match(gated, gihs)}

    # Gate sensitivity: how much do the conclusions depend on the thresholds?
    sens = []
    for months in (3, 4, 5, 6, 8):
        for days in (5, 10, 20):
            g = grid_cluster(cells, months, days)
            nr = near_sources(df, g)
            row = {"months": months, "days": days,
                   "sources": int(g.loc[g["source"] >= 0, "source"].nunique())}
            for name, m in masks.items():
                row[name] = round(float(nr[m].mean()), 3)
            row["type2_recall"] = round(float(nr[t2].mean()), 3) if t2.any() else None
            if gihs is not None:
                match = gihs_match(g, gihs)
                row["gihs_recall"] = match["recall_active_2021"]
                row["on_gihs"] = match["sources_on_gihs"]
            sens.append(row)
    results["gate_sensitivity"] = sens
    results["platform"] = {"python": platform.python_version(), "machine": platform.machine(),
                           "processor": platform.processor()}

    print(">> figures")
    figure_regions(df, widths, summaries, OUT / "fig1_regions.png")
    figure_recurrence(cells, OUT / "fig2_recurrence.png")
    if mem:
        figure_memory(mem, OUT / "fig3_memory.png")
    figure_india(df, gated, OUT / "fig4_india.png")

    (OUT / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    write_tables(results, OUT / "tables.md")
    print(f">> wrote {OUT.relative_to(REPO)}")
    return 0


def write_tables(r: dict, path: Path) -> None:
    lines = ["# Stage 1.5 - generated tables", "",
             f"Year {r['year']}; {r['data']['detections']:,} VIIRS detections; "
             f"{r['data']['night_share']:.1%} at night.", "",
             "| Run | Region | Detections | Skip Road A | In blobs >10 km | Widest source |",
             "|---|---|---|---|---|---|"]
    for run, s in r["summaries"].items():
        for region, v in s["regions"].items():
            lines.append(f"| {run} | {region} | {v['detections']:,} | {v['skip_road_a']:.1%} "
                         f"| {v['in_landscape']:.1%} | {v['widest_km']:g} km |")
    if r.get("memory"):
        lines += ["", "| Run | Points | Seconds | Peak extra MB |", "|---|---|---|---|"]
        for run, m in r["memory"].items():
            lines.append(f"| {run} | {m['n_points']:,} | {m['secs']} | {m['delta_mb']:,} |")
    has_gihs = "gihs_recall" in r["gate_sensitivity"][0]
    extra = " | GIHS recall (active 2021) | Sources on a GIHS site |" if has_gihs else " |"
    lines += ["", "Share of each region's detections within 500 m of a registered source:", "",
              "| Gate months | Gate days | Sources | " + " | ".join(REGIONS)
              + " | FIRMS type=2 recall" + extra,
              "|---|---|---|" + "---|" * len(REGIONS) + "---|" + ("---|---|" if has_gihs else "")]
    for row in r["gate_sensitivity"]:
        cells = (f"| {row['months']} | {row['days']} | {row['sources']:,} | "
                 + " | ".join(f"{row[n]:.1%}" for n in REGIONS)
                 + f" | {row['type2_recall']:.1%} |")
        if has_gihs:
            cells += f" {row['gihs_recall']:.1%} | {row['on_gihs']:.1%} |"
        lines.append(cells)
    for run, a in r.get("absorption", {}).items():
        lines += ["", f"Run {run}: {a['mostly_one_off_fires']} of {a['sources_checked']} "
                  f"persistent sources sit in a raw cluster made mostly of one-off fires "
                  f"(median contamination {a['median_contamination']:.0%})."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
