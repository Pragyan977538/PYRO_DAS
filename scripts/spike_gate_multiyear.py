"""Stage 1.5b - does the recurrence gate hold on several real years?

The one-year spike proposed a gate -- a 375 m cell burns in >= 4 months on >= 10
days within a year, in at least 2 years -- but one year of data cannot exercise
the "2 years" part. This reruns the gate on 2021-2023 and sweeps it:

  months per year   3, 4, 5, 6
  days per year     5, 10, 20
  years             1, 2, 3   (how many of the three years must pass)

For every setting it reports the Punjab paddy belt's misrouted share, GIHS recall
and precision, FIRMS type=2 recall, and whether the three refineries the demo leans
on -- Reliance Jamnagar, Nayara Vadinar, HMEL Bathinda -- come out as sources at
their published coordinates. It also reruns the original raw DBSCAN on all three
years, to see whether chaining grows with history and whether memory followed the
one-year extrapolation.

No database. Writes reports/stage1_5b/{results.json, tables.md, *.png} and the
recommended registry to data/interim/spike_multiyear/ for the label census.

    python scripts/spike_gate_multiyear.py [--years 2021 2022 2023] [--skip-raw]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import spike_cluster as sc  # noqa: E402

from firewatch.config import settings  # noqa: E402

YEARS = (2021, 2022, 2023)
SWEEP_MONTHS = (3, 4, 5, 6)
SWEEP_DAYS = (5, 10, 20)
SWEEP_YEARS = (1, 2, 3)
PUNJAB_LIMIT = 0.02      # misrouted share of the paddy belt we will accept
PROPOSED = (2, 4, 10)    # (years, months, days) proposed by the one-year spike, fixed
                         # before this sweep ran
SITE_RADIUS_M = 3000     # refinery complexes span kilometres; published points are nominal
RAW_LIMIT_GB = 12        # skip the raw DBSCAN rerun if its neighbour lists exceed this

# Published coordinates (Wikipedia infoboxes). The demo leans on these three.
SITES = {
    "Reliance Jamnagar": (22.34806, 69.86889),
    "Nayara Vadinar": (22.33167, 69.74722),
    "HMEL Bathinda": (29.93056, 74.94583),
}
PUNJAB = "Punjab paddy belt"
JHARIA = "Jharia coalfield"

OUT = REPO / "reports" / "stage1_5b"


def multi_year_cells(df: pd.DataFrame):
    """Per-cell totals plus per-year recurrence (distinct days, distinct months)."""
    cx = np.floor(df["x"].to_numpy() / sc.CELL_M).astype(np.int64)
    cy = np.floor(df["y"].to_numpy() / sc.CELL_M).astype(np.int64)
    frame = pd.DataFrame({
        "cell": cx * 100_000 + cy, "year": df["year"].to_numpy(),
        "day": df["day"].to_numpy(), "month": df["month"].to_numpy(),
        "x": df["x"].to_numpy(), "y": df["y"].to_numpy(),
        "lat": df["latitude"].to_numpy(), "lon": df["longitude"].to_numpy(),
        "type2": (df["firms_type"] == 2).fillna(False).to_numpy(dtype=bool),
    })
    per_year = frame.groupby(["cell", "year"]).agg(days=("day", "nunique"),
                                                   months=("month", "nunique"))
    days = per_year["days"].unstack(fill_value=0)
    months = per_year["months"].unstack(fill_value=0)
    cells = frame.groupby("cell").agg(
        n_det=("x", "size"), x=("x", "mean"), y=("y", "mean"),
        lat=("lat", "mean"), lon=("lon", "mean"), type2_frac=("type2", "mean"))
    return cells, days, months


def gate(cells: pd.DataFrame, days: pd.DataFrame, months: pd.DataFrame,
         m: int, d: int, y: int) -> pd.DataFrame:
    """Keep cells that burn in >= m months on >= d days in at least y years."""
    passing_years = ((months >= m) & (days >= d)).sum(axis=1)
    keep = passing_years.index[passing_years >= y]
    return sc.cluster_cells(cells.loc[keep].copy())


def site_check(gated: pd.DataFrame, near: np.ndarray, det_x: np.ndarray,
               det_y: np.ndarray, lat: float, lon: float) -> dict:
    """Is there a registered source at a published site, and how much of the
    site's own detections does it cover?"""
    sx, sy = sc.to_metres(np.array([lat]), np.array([lon]))
    reg = gated[gated["source"] >= 0]
    nearest = float(np.hypot(reg["x"] - sx[0], reg["y"] - sy[0]).min()) if len(reg) else np.inf
    around = np.hypot(det_x - sx[0], det_y - sy[0]) < SITE_RADIUS_M
    return {"source_km": round(nearest / 1000, 2),
            "found": bool(nearest < SITE_RADIUS_M),
            "detections": int(around.sum()),
            "coverage": round(float(near[around].mean()), 3) if around.any() else None}


def sweep(df, cells, days, months, masks, gihs, hmel_zone) -> tuple[list[dict], dict]:
    det_x, det_y = df["x"].to_numpy(), df["y"].to_numpy()
    t2 = (df["firms_type"] == 2).fillna(False).to_numpy(dtype=bool)
    punjab_outside_hmel = masks[PUNJAB] & ~hmel_zone
    rows, gated_by_key = [], {}
    for y in SWEEP_YEARS:
        for m in SWEEP_MONTHS:
            for d in SWEEP_DAYS:
                g = gate(cells, days, months, m, d, y)
                near = sc.near_sources(df, g)
                row = {"years": y, "months": m, "days": d,
                       "sources": int(g.loc[g["source"] >= 0, "source"].nunique()),
                       "punjab_all": round(float(near[masks[PUNJAB]].mean()), 4),
                       "punjab_misrouted": round(float(near[punjab_outside_hmel].mean()), 4),
                       "jharia": round(float(near[masks[JHARIA]].mean()), 4),
                       "type2_recall": round(float(near[t2].mean()), 4)}
                for name, (lat, lon) in SITES.items():
                    row[name] = site_check(g, near, det_x, det_y, lat, lon)
                if gihs is not None:
                    match = sc.gihs_match(g, gihs)
                    row["gihs_recall"] = match["recall_active_2021"]
                    row["gihs_recall_all"] = match["recall_confirmed"]
                    row["on_gihs"] = match["sources_on_gihs"]
                rows.append(row)
                gated_by_key[(y, m, d)] = g
                print(f"   y>={y} m>={m} d>={d}: {row['sources']:4d} sources, "
                      f"punjab misrouted {row['punjab_misrouted']:.2%}, "
                      f"gihs recall {row.get('gihs_recall')}")
    return rows, gated_by_key


def balance(r: dict) -> float:
    """F1 of GIHS recall and the on-GIHS share (a lower bound on precision).

    Recall alone rewards letting everything in: the loosest gate recovers the most
    GIHS sites while a quarter of its sources are not on any. Both errors cost
    something -- a missed plant loses its baseline, a registered crop hotspot gets
    one -- so the two are balanced.
    """
    rec, prec = r.get("gihs_recall") or 0.0, r.get("on_gihs") or 0.0
    return 2 * rec * prec / (rec + prec) if rec + prec else 0.0


def recommend(rows: list[dict]) -> dict:
    """The setting we propose for Stage 3.

    Multi-year recurrence is a design requirement (it keeps a single long accident
    out of the registry), so only years >= 2 qualify, and a setting must keep the
    paddy belt within the misrouting limit and find all three refineries. Among
    those, the gate proposed from one year (PROPOSED) was fixed before this sweep
    ran; it is kept if it is within one F1 point of the best, which guards against
    tuning to noise in a 36-setting sweep. Otherwise the best F1 wins.
    """
    ok = [r for r in rows if r["years"] >= 2 and r["punjab_misrouted"] <= PUNJAB_LIMIT
          and all(r[s]["found"] for s in SITES)]
    if not ok:
        return {}
    best = max(ok, key=lambda r: (balance(r), -r["sources"]))
    proposed = next((r for r in ok if (r["years"], r["months"], r["days"]) == PROPOSED), None)
    chosen = proposed if proposed and balance(best) - balance(proposed) <= 0.01 else best
    return {**chosen, "f1": round(balance(chosen), 4),
            "best_f1_setting": {k: best[k] for k in ("years", "months", "days")},
            "best_f1": round(balance(best), 4),
            "kept_proposed": chosen is proposed}


def raw_rerun(df: pd.DataFrame, masks: dict, work: Path) -> dict | None:
    """The original raw DBSCAN on all years: does chaining grow with history?"""
    xy = df[["x", "y"]].to_numpy()
    total = sc.neighbour_total(xy)
    est_gb = total * 8 / 2**30
    print(f"   raw rerun: {total:,} neighbour entries (~{est_gb:.1f} GB)")
    out = {"points": int(len(xy)), "neighbour_entries": int(total),
           "neighbour_lists_gb": round(est_gb, 2)}
    if est_gb > RAW_LIMIT_GB:
        out["skipped"] = f"neighbour lists would exceed {RAW_LIMIT_GB} GB"
        return out
    npz = work / "points_multiyear.npz"
    np.savez(npz, x=df["x"].to_numpy(), y=df["y"].to_numpy(),
             sensor=np.zeros(len(df), dtype=np.int8))
    stats, labels = sc.run_child("A_ball", npz, work)
    x, y = df["x"].to_numpy(), df["y"].to_numpy()
    table = sc.cluster_widths(labels, x, y)
    width = sc.point_width(labels, table, sc.MIN_SAMPLES)
    summary = sc.summarise("A_multiyear", width, masks)
    sizable = table[table["n"] >= sc.MIN_SAMPLES]
    out.update({
        "memory": stats, "clusters": int(len(sizable)),
        "wider_than_10km": int((sizable["width_km"] > sc.LANDSCAPE_KM).sum()),
        "widest_km": round(float(sizable["width_km"].max()), 1),
        "regions": summary["regions"],
        "india_skip_road_a": summary["india_skip_road_a"],
    })
    return out


# ----------------------------------------------------------------------- figures

def figure_sites(df: pd.DataFrame, near: np.ndarray, path: Path, title: str) -> None:
    import matplotlib.pyplot as plt

    views = {
        "Jamnagar: Reliance and Nayara refineries": (22.27, 22.41, 69.68, 69.94),
        "Bathinda: HMEL refinery in the paddy belt": (29.87, 29.99, 74.87, 75.03),
    }
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.6), facecolor=sc.SURFACE)
    lat, lon = df["latitude"].to_numpy(), df["longitude"].to_numpy()
    for ax, (name, (la0, la1, lo0, lo1)) in zip(axes, views.items(), strict=True):
        box = (lat >= la0) & (lat <= la1) & (lon >= lo0) & (lon <= lo1)
        other, hit = box & ~near, box & near
        ax.scatter(lon[other], lat[other], s=6, c=sc.ROAD_A, linewidths=0,
                   label=f"no source: Road A ({other.sum():,})")
        ax.scatter(lon[hit], lat[hit], s=6, c="#2a78d6", linewidths=0,
                   label=f"within 500 m of a source ({hit.sum():,})")
        for site, (slat, slon) in SITES.items():
            if la0 <= slat <= la1 and lo0 <= slon <= lo1:
                ax.plot(slon, slat, marker="+", ms=14, mew=2, color=sc.INK, ls="")
                ax.annotate(f"{site}\n(published location)", (slon, slat),
                            xytext=(8, 8), textcoords="offset points", fontsize=8,
                            color=sc.INK2)
        ax.set_xlim(lo0, lo1)
        ax.set_ylim(la0, la1)
        ax.set_aspect(1 / np.cos(np.radians((la0 + la1) / 2)))
        sc._style(ax, name)
        ax.legend(frameon=False, fontsize=8, labelcolor=sc.INK2, loc="lower left")
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=sc.INK)
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=sc.SURFACE)
    plt.close(fig)


def figure_raw_blob(df: pd.DataFrame, labels: np.ndarray, path: Path) -> None:
    """What the original plan does to Punjab with three years of history."""
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    la0, la1, lo0, lo1 = 28.8, 32.6, 73.2, 77.6
    lat, lon = df["latitude"].to_numpy(), df["longitude"].to_numpy()
    box = (lat >= la0) & (lat <= la1) & (lon >= lo0) & (lon <= lo1)
    sizes = pd.Series(labels[box & (labels >= 0)]).value_counts()
    biggest = sizes.index[0]
    big = box & (labels == biggest)
    other = box & (labels >= 0) & ~big
    noise = box & (labels < 0)
    stubble = np.isin(df["month"].to_numpy()[big], [4, 5, 10, 11]).mean()

    fig, ax = plt.subplots(figsize=(8, 7.5), facecolor=sc.SURFACE)
    ax.scatter(lon[noise], lat[noise], s=0.4, c=sc.ROAD_A, linewidths=0, rasterized=True)
    ax.scatter(lon[other], lat[other], s=0.4, c="#6da7ec", linewidths=0, rasterized=True)
    ax.scatter(lon[big], lat[big], s=0.4, c="#0d366b", linewidths=0, rasterized=True)
    handles = [
        Line2D([], [], marker="o", ls="", color="#0d366b",
               label=f"one cluster: {big.sum():,} detections, {stubble:.0%} in stubble months"),
        Line2D([], [], marker="o", ls="", color="#6da7ec", label="other raw clusters"),
        Line2D([], [], marker="o", ls="", color=sc.ROAD_A, label="unclustered"),
    ]
    ax.legend(handles=handles, frameon=False, fontsize=8, labelcolor=sc.INK2,
              loc="lower left")
    ax.set_xlim(lo0, lo1)
    ax.set_ylim(la0, la1)
    ax.set_aspect(1 / np.cos(np.radians((la0 + la1) / 2)))
    sc._style(ax, "The original plan on 2021-2023: raw DBSCAN turns the paddy belt "
                  "into one 'source'")
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=sc.SURFACE)
    plt.close(fig)


def figure_tradeoff(rows: list[dict], best: dict, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 5.4), facecolor=sc.SURFACE)
    for color, y in zip(sc.REGION_COLORS, SWEEP_YEARS, strict=True):
        sel = [r for r in rows if r["years"] == y and r.get("gihs_recall") is not None]
        ax.scatter([100 * r["on_gihs"] for r in sel], [100 * r["gihs_recall"] for r in sel],
                   s=40, c=color, edgecolors=sc.SURFACE, linewidths=1.5,
                   label=f"burns in >= {y} of 3 years")
    if best:
        ax.scatter([100 * best["on_gihs"]], [100 * best["gihs_recall"]], s=220,
                   facecolors="none", edgecolors=sc.INK, linewidths=1.5)
        ax.annotate(f"proposed: >= {best['months']} months, >= {best['days']} days,\n"
                    f"in >= {best['years']} years (F1 {best['f1']:.0%})",
                    (100 * best["on_gihs"], 100 * best["gihs_recall"]),
                    xytext=(-190, -40), textcoords="offset points", fontsize=8,
                    color=sc.INK2, arrowprops={"arrowstyle": "-", "color": sc.INK2, "lw": 0.8})
    worst_punjab = max(r["punjab_misrouted"] for r in rows)
    ax.set_xlabel("sources on an imagery-verified GIHS site (%) - a lower bound on precision",
                  fontsize=9, color=sc.INK2)
    ax.set_ylabel("GIHS industrial sites recovered (%)", fontsize=9, color=sc.INK2)
    ax.grid(True, color=sc.GRID, lw=0.8)
    ax.set_axisbelow(True)
    sc._style(ax, f"36 gate settings on 2021-2023 - every one misroutes <= "
                  f"{worst_punjab:.1%} of the paddy belt")
    ax.legend(frameon=False, fontsize=8, labelcolor=sc.INK2, loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=sc.SURFACE)
    plt.close(fig)


# -------------------------------------------------------------------------- main

def write_tables(r: dict, path: Path) -> None:
    lines = ["# Stage 1.5b - generated tables", "",
             f"Years {r['years']}; {r['data']['detections']:,} VIIRS detections.", "",
             "| Years | Months | Days | Sources | Punjab misrouted | Punjab all | Jharia "
             "| Reliance | Nayara | HMEL | FIRMS type=2 | GIHS recall (active) "
             "| GIHS recall (all) | On GIHS | F1 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def site(v: dict) -> str:
        cov = "-" if v["coverage"] is None else f"{v['coverage']:.0%}"
        return f"{'yes' if v['found'] else 'NO'} ({cov})"

    for row in r["sweep"]:
        lines.append(
            f"| {row['years']} | {row['months']} | {row['days']} | {row['sources']:,} "
            f"| {row['punjab_misrouted']:.2%} | {row['punjab_all']:.2%} | {row['jharia']:.1%} "
            + "".join(f"| {site(row[s])} " for s in SITES)
            + f"| {row['type2_recall']:.1%} | {row.get('gihs_recall', 0):.1%} "
            f"| {row.get('gihs_recall_all', 0):.1%} | {row.get('on_gihs', 0):.1%} "
            f"| {balance(row):.1%} |")
    rec = r.get("recommended") or {}
    if rec:
        lines += ["", f"Recommended: >= {rec['months']} months, >= {rec['days']} days, in >= "
                  f"{rec['years']} years (F1 {rec['f1']:.1%}; kept the pre-registered "
                  f"proposal: {rec['kept_proposed']}). Best F1 in the sweep: "
                  f"{rec['best_f1_setting']} at {rec['best_f1']:.1%}."]
    raw = r.get("raw_multiyear") or {}
    if raw.get("regions"):
        p = raw["regions"][PUNJAB]
        lines += ["", f"Raw DBSCAN on all years: {raw['clusters']:,} clusters, "
                  f"{raw['wider_than_10km']} wider than 10 km, widest {raw['widest_km']} km; "
                  f"Punjab paddy belt {p['skip_road_a']:.1%} inside a source, "
                  f"{p['in_landscape']:.1%} in blobs > 10 km; peak memory "
                  f"+{raw['memory']['delta_mb']:,} MB in {raw['memory']['secs']} s."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", type=int, nargs="+", default=list(YEARS))
    ap.add_argument("--skip-raw", action="store_true", help="skip the raw DBSCAN rerun")
    args = ap.parse_args()

    cfg = settings()
    raw_dir = cfg.raw_dir
    work = cfg.data_dir / "interim" / "spike_multiyear"
    work.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f">> loading {args.years}")
    paths = [p for y in args.years for p in sc.fetch(y, raw_dir / "firms")]
    df = sc.load(paths)
    df["year"] = df["acq_datetime"].dt.year.astype(np.int16)
    masks = {name: sc.region_mask(df, box) for name, box in sc.REGIONS.items()}
    hx, hy = sc.to_metres(np.array([SITES["HMEL Bathinda"][0]]),
                          np.array([SITES["HMEL Bathinda"][1]]))
    hmel_zone = np.hypot(df["x"].to_numpy() - hx[0], df["y"].to_numpy() - hy[0]) < SITE_RADIUS_M

    results: dict = {"years": args.years, "data": {
        "detections": int(len(df)),
        "by_year": {int(k): int(v) for k, v in df["year"].value_counts().sort_index().items()},
        "night_share": round(float(df["night"].mean()), 4),
        "punjab_detections": int(masks[PUNJAB].sum()),
        "punjab_outside_hmel_zone": int((masks[PUNJAB] & ~hmel_zone).sum()),
    }}
    print(f"   {len(df):,} detections")

    print(">> cells and per-year recurrence")
    cells, days, months = multi_year_cells(df)
    results["cells"] = int(len(cells))
    gihs = sc.load_gihs(raw_dir)

    print(">> gate sweep")
    rows, gated_by_key = sweep(df, cells, days, months, masks, gihs, hmel_zone)
    results["sweep"] = rows
    best = recommend(rows)
    results["recommended"] = best

    if best:
        key = (best["years"], best["months"], best["days"])
        g = gated_by_key[key]
        reg = g[g["source"] >= 0]
        reg[["source", "lat", "lon", "n_det", "width_km"]].to_csv(
            work / "registry_cells.csv", index_label="cell")
        near = sc.near_sources(df, g)
        figure_sites(df, near, OUT / "fig1_sites.png",
                     f"Proposed gate on {args.years[0]}-{args.years[-1]}: >= {best['months']} "
                     f"months, >= {best['days']} days, in >= {best['years']} years")
        figure_tradeoff(rows, best, OUT / "fig2_tradeoff.png")

    previous = OUT / "results.json"
    if not args.skip_raw:
        print(">> raw DBSCAN on all years (the original plan)")
        results["raw_multiyear"] = raw_rerun(df, masks, work)
    elif previous.exists():
        # The raw rerun takes minutes and several GB; keep the last measurement.
        results["raw_multiyear"] = json.loads(previous.read_bytes()).get("raw_multiyear")
    raw_labels = work / "labels_A_ball.npy"
    if raw_labels.exists():
        labels = np.load(raw_labels)
        if len(labels) == len(df):
            figure_raw_blob(df, labels, OUT / "fig3_raw_blob.png")

    (OUT / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    write_tables(results, OUT / "tables.md")
    print(f">> wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
