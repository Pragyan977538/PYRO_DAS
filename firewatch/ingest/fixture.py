"""Synthetic FIRMS-shaped test fixture, and a spike injector for real histories.

Two jobs, both for tests:

* ``generate()`` builds a small, deterministic world whose files look exactly like
  the FIRMS archive -- both instruments, SP and NRT with an overlap -- plus
  VNF-shaped temperatures, an ERA5-shaped cloud table and the ground truth.
  ``MOCK_MODE=1`` runs the pipeline on it, offline.
* ``inject_spikes()`` adds excursions of known size and time to a *real* source's
  history. That is how anomaly detection is tested: against real FRP variability,
  not a distribution we invented.

The world's structure is what matters, not its scale. The first synthetic benchmark
modelled stubble and forest fires as tight point sources, and that one mistake hid
the landscape-chaining problem until real data exposed it. Here biomass is diffuse:
thousands of fields packed into a belt, each burning a day or two a season and
recurring year after year, and forest fires that spread across pixels. The numbers
it is tuned to are the measured ones in CLAUDE.md: ~30% of detections at night,
intermittent flares, temperatures only at night.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from firewatch.grid import era5_cell, offset

VIIRS_COLUMNS = ["latitude", "longitude", "bright_ti4", "scan", "track", "acq_date",
                 "acq_time", "satellite", "instrument", "confidence", "version",
                 "bright_ti5", "frp", "daynight", "type"]
MODIS_COLUMNS = ["latitude", "longitude", "brightness", "scan", "track", "acq_date",
                 "acq_time", "satellite", "instrument", "confidence", "version",
                 "bright_t31", "frp", "daynight", "type"]
# Names as documented for VNF; verify against the real header when the licence
# arrives -- they have shifted across VNF versions.
VNF_COLUMNS = ["Date_Mscan", "Lat_GMTCO", "Lon_GMTCO", "Temp_BB", "Area_BB", "RH",
               "Cloud_Mask", "Sat"]

DEFAULT_START = date(2022, 1, 1)
DEFAULT_END = date(2023, 12, 31)
SP_LAG_DAYS = 15        # the archive (SP) runs this far behind the period's end ...
NRT_DAYS = 45           # ... while NRT covers the last 45 days, so they overlap
PIXEL_JITTER_M = 110    # pixel-centre scatter around a fixed hot spot
NRT_SHIFT_DEG = 0.0004  # reprocessing moves NRT pixel centres slightly off SP's

PADDY_BOX = (30.10, 30.30, 75.20, 75.45)   # lat0, lat1, lon0, lon1
N_FIELDS = 3000                            # ~6 fields per km2: a real paddy belt
FOREST_BOX = (21.00, 21.30, 82.00, 82.30)
N_FOREST_FIRES = 80                        # per year


@dataclass(frozen=True)
class Sensor:
    code: str            # FIRMS satellite code in the archive
    nrt_code: str        # ... and in NRT/API files
    instrument: str      # VIIRS | MODIS
    slug: str            # archive file prefix
    nrt_product: str     # FIRMS API product name
    day_utc: float       # overpass hour, UTC, over India
    night_utc: float
    sensitivity: float   # relative detection probability; MODIS's 1 km pixels see less


SENSORS = (
    Sensor("N", "N", "VIIRS", "viirs-snpp", "VIIRS_SNPP_NRT", 8.2, 20.2, 1.0),
    Sensor("N20", "N20", "VIIRS", "viirs-jpss1", "VIIRS_NOAA20_NRT", 7.4, 19.4, 1.0),
    Sensor("Aqua", "A", "MODIS", "modis", "MODIS_NRT", 8.0, 20.0, 0.45),
    Sensor("Terra", "T", "MODIS", "modis", "MODIS_NRT", 5.5, 17.5, 0.45),
)
_BY_CODE = {s.code: s for s in SENSORS}


@dataclass(frozen=True)
class SourceSpec:
    """A persistent thermal source, and the class Model 1 should give it."""

    name: str
    cls: str                 # oil_gas | heavy_industry | mining
    lat: float
    lon: float
    spots: int               # distinct hot spots: a coalfield has many, a flare one
    spread_m: float          # how far spots sit from the centre
    p_detect: float          # chance per clear pass that a spot is detected
    night_boost: float       # small hot sources stand out against a cold night
    frp_median: float        # MW, lognormal
    frp_sigma: float
    temp_k: float            # VNF blackbody temperature
    temp_sd: float
    area_m2: float           # VNF source area
    vnf_fit: float           # chance VNF fits a temperature to a night detection
    facility_tag: str        # OSM tag of the facility around it (mock OSM)
    # Active window as (days before the period's end it starts, ... it stops);
    # None means the whole period.
    active: tuple[int, int] | None = None
    expected: str = "registered"


SOURCES = (
    SourceSpec("flare_jamnagar", "oil_gas", 22.340, 69.870, 1, 0, 0.30, 2.0, 12, 0.6,
               1750, 60, 15, 0.8, "industrial=refinery"),
    SourceSpec("flare_vadinar", "oil_gas", 22.330, 69.750, 1, 0, 0.22, 2.0, 6, 0.6,
               1720, 60, 10, 0.8, "industrial=refinery"),
    # A refinery inside the paddy belt, like HMEL at Bathinda: the registry must
    # keep it while sending the fields around it to Road A.
    SourceSpec("refinery_in_belt", "oil_gas", 30.200, 75.320, 1, 0, 0.30, 1.8, 15, 0.6,
               1700, 70, 20, 0.8, "industrial=refinery"),
    SourceSpec("steel_works", "heavy_industry", 21.190, 81.390, 3, 600, 0.35, 1.0, 40,
               0.5, 1300, 110, 400, 0.6, "man_made=works;product=steel"),
    SourceSpec("power_plant", "heavy_industry", 22.790, 86.200, 2, 400, 0.30, 1.0, 25,
               0.5, 1200, 100, 300, 0.5, "power=plant;plant:source=coal"),
    SourceSpec("coalfield", "mining", 23.750, 86.400, 8, 3000, 0.25, 1.2, 8, 0.7,
               900, 80, 2500, 0.3, "landuse=quarry;resource=coal"),
    # Commissioned late in the period: a Road A site that should become a
    # provisional source, still alerting.
    SourceSpec("new_flare", "oil_gas", 22.450, 69.950, 1, 0, 0.40, 2.0, 10, 0.6,
               1760, 60, 15, 0.8, "man_made=petroleum_well", active=(300, 0),
               expected="provisional"),
    # A Baghjan-like blowout: an accident that burns for five months. It must stay
    # an alert for its whole life and never become "normal".
    SourceSpec("blowout", "oil_gas", 27.580, 95.390, 1, 0, 0.90, 1.0, 300, 0.5,
               1400, 90, 3000, 0.7, "man_made=petroleum_well", active=(205, 46),
               expected="never_normal"),
)
_SPEC = {s.name: s for s in SOURCES}


@dataclass
class Fixture:
    """A generated world: FIRMS-shaped files, VNF, cloud and the ground truth."""

    start: date
    end: date
    seed: int
    firms: dict[str, pd.DataFrame]
    vnf: pd.DataFrame
    cloud: pd.DataFrame
    truth: dict[str, pd.DataFrame] = field(default_factory=dict)

    def firms_frame(self, instrument: str | None = None,
                    product: str | None = None) -> pd.DataFrame:
        """Concatenate the FIRMS files of one instrument, optionally one product.

        Frames of different instruments are never mixed: their column names differ
        on purpose, and ``normalise_firms`` rejects a frame that has both.
        """
        keys = [k for k in self.firms
                if (instrument is None or _instrument_of(k) == instrument)
                and (product is None or _product_of(k) == product)]
        if instrument is None and {_instrument_of(k) for k in keys} == {"VIIRS", "MODIS"}:
            raise ValueError("pass instrument=: VIIRS and MODIS frames can't be mixed")
        return pd.concat([self.firms[k] for k in keys], ignore_index=True)

    def write(self, out_dir: Path) -> list[Path]:
        """Write every table as CSV under ``out_dir``, laid out like the real data."""
        out_dir = Path(out_dir)
        written = []
        tables = {**self.firms, "vnf/vnf.csv": self.vnf,
                  "observability/cloud.csv": self.cloud,
                  **{f"truth/{name}.csv": frame for name, frame in self.truth.items()}}
        for rel, frame in tables.items():
            path = out_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(path, index=False)
            written.append(path)
        return written


def _instrument_of(key: str) -> str:
    return "MODIS" if "modis" in key.lower() else "VIIRS"


def _product_of(key: str) -> str:
    return "NRT" if key.startswith("firms_nrt/") else "SP"


# --------------------------------------------------------------------------- world

class _World:
    """Dates, the random stream, and the sky.

    Cloud is drawn per ERA5 cell from a generator seeded by (seed, cell), so the
    sky over a cell is the same whatever order the cells are first asked about.
    """

    def __init__(self, start: date, end: date, seed: int) -> None:
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.dates = pd.date_range(start, end, freq="D", tz="UTC")
        self.n_days = len(self.dates)
        months = self.dates.month.to_numpy()
        self._mean_cloud = np.where(np.isin(months, [6, 7, 8, 9]), 0.72, 0.20)
        self._sky: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def _cell_sky(self, cell: int) -> tuple[np.ndarray, np.ndarray]:
        if cell not in self._sky:
            g = np.random.default_rng([self.seed, int(cell)])
            m = np.repeat(self._mean_cloud[:, None], 2, axis=1)   # (days, day/night)
            frac = g.beta(m * 4, (1 - m) * 4)
            clear = g.random(frac.shape) >= frac
            self._sky[cell] = (frac, clear)
        return self._sky[cell]

    def clear(self, cells: np.ndarray, day: np.ndarray, night: np.ndarray) -> np.ndarray:
        out = np.empty(len(cells), dtype=bool)
        for cell in np.unique(cells):
            sel = cells == cell
            _, clear = self._cell_sky(int(cell))
            out[sel] = clear[day[sel], night[sel].astype(int)]
        return out

    def force_clear(self, cell: int, day: int, night: bool) -> None:
        frac, clear = self._cell_sky(int(cell))
        frac[day, int(night)] = min(frac[day, int(night)], 0.05)
        clear[day, int(night)] = True

    def window(self, active: tuple[int, int] | None) -> tuple[int, int]:
        if active is None:
            return 0, self.n_days - 1
        before_start, before_stop = active
        last = self.n_days - 1
        return max(0, last - before_start), max(0, last - before_stop)

    def cloud_table(self) -> pd.DataFrame:
        frames = []
        for cell, (frac, _) in sorted(self._sky.items()):
            for night, dn in ((0, "D"), (1, "N")):
                frames.append(pd.DataFrame({
                    "cell_id": cell, "obs_date": self.dates.date, "daynight": dn,
                    "cloud_frac": np.round(frac[:, night], 3)}))
        return pd.concat(frames, ignore_index=True)


def _observe(world: _World, lat, lon, day, night, p, frp_median, frp_sigma,
             kind: str, origin) -> pd.DataFrame:
    """Turn fire activity into detections.

    Every sensor's pass over an active fire detects it with probability
    ``p * sensitivity``, if that cell's sky was clear on that pass.
    """
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    day, night = np.asarray(day, int), np.asarray(night, bool)
    p = np.broadcast_to(np.asarray(p, float), lat.shape)
    frp_median = np.broadcast_to(np.asarray(frp_median, float), lat.shape)
    frp_sigma = np.broadcast_to(np.asarray(frp_sigma, float), lat.shape)
    origin = np.broadcast_to(np.asarray(origin, object), lat.shape)
    clear = world.clear(era5_cell(lat, lon), day, night)
    rng = world.rng
    frames = []
    for s in SENSORS:
        hit = clear & (rng.random(lat.size) < np.minimum(p * s.sensitivity, 0.97))
        k = int(hit.sum())
        if not k:
            continue
        hour = np.where(night[hit], s.night_utc, s.day_utc)
        minutes = np.round(hour * 60 + rng.normal(0, 6, k))
        dx, dy = rng.normal(0, PIXEL_JITTER_M, (2, k))
        la, lo = offset(lat[hit], lon[hit], dx, dy)
        frames.append(pd.DataFrame({
            "sensor": s.code,
            "acq_datetime": world.dates[day[hit]] + pd.to_timedelta(minutes, unit="min"),
            "day": day[hit], "night": night[hit], "latitude": la, "longitude": lo,
            "frp": frp_median[hit] * np.exp(rng.normal(0, frp_sigma[hit])),
            "origin_kind": kind, "origin_id": origin[hit],
        }))
    return pd.concat(frames, ignore_index=True) if frames else _empty()


_DETECTION_COLUMNS = ["sensor", "acq_datetime", "day", "night", "latitude", "longitude",
                      "frp", "origin_kind", "origin_id"]


def _empty() -> pd.DataFrame:
    """No detections, with the columns every generator returns."""
    return pd.DataFrame({c: pd.Series(dtype=object) for c in _DETECTION_COLUMNS})


# ------------------------------------------------------------------------ entities

def _persistent(world: _World) -> pd.DataFrame:
    rng, frames = world.rng, []
    for spec in SOURCES:
        first, last = world.window(spec.active)
        if last < first:
            continue
        radius = spec.spread_m * np.sqrt(rng.uniform(0, 1, spec.spots))
        angle = rng.uniform(0, 2 * math.pi, spec.spots)
        if spec.spots == 1:
            radius[:] = 0.0
        s_lat, s_lon = offset(np.full(spec.spots, spec.lat), np.full(spec.spots, spec.lon),
                              radius * np.cos(angle), radius * np.sin(angle))
        spot, day, night = np.meshgrid(np.arange(spec.spots), np.arange(first, last + 1),
                                       [False, True], indexing="ij")
        spot, day, night = spot.ravel(), day.ravel(), night.ravel()
        p = spec.p_detect * np.where(night, spec.night_boost, 1.0)
        frames.append(_observe(world, s_lat[spot], s_lon[spot], day, night, p,
                               spec.frp_median, spec.frp_sigma, "source", spec.name))
    return pd.concat(frames, ignore_index=True)


def _season_days(world: _World, year: int, first: tuple[int, int],
                 last: tuple[int, int]) -> tuple[int, int] | None:
    lo = pd.Timestamp(year, *first, tz="UTC")
    hi = pd.Timestamp(year, *last, tz="UTC")
    idx = np.flatnonzero((world.dates >= lo) & (world.dates <= hi))
    return (int(idx[0]), int(idx[-1])) if idx.size else None


def _paddy(world: _World) -> pd.DataFrame:
    """Dense fields that burn a day or two a season, the same fields every year."""
    rng = world.rng
    la0, la1, lo0, lo1 = PADDY_BOX
    f_lat = rng.uniform(la0, la1, N_FIELDS)
    f_lon = rng.uniform(lo0, lo1, N_FIELDS)
    lat, lon, day, night, p, fid = [], [], [], [], [], []
    seasons = ((0.70, (10, 10), (11, 25)),   # paddy stubble, kharif
               (0.35, (4, 15), (5, 20)))     # wheat stubble, rabi
    for year in sorted(set(world.dates.year)):
        for p_burn, first, last in seasons:
            span = _season_days(world, year, first, last)
            if span is None:
                continue
            burning = np.flatnonzero(rng.random(N_FIELDS) < p_burn)
            burn_day = rng.integers(span[0], span[1] + 1, burning.size)
            # the burn day's afternoon pass, a faint night pass, a smouldering next day
            for offset_day, is_night, prob in ((0, False, 0.75), (0, True, 0.08),
                                               (1, False, 0.20)):
                d = burn_day + offset_day
                ok = d < world.n_days
                lat.append(f_lat[burning[ok]])
                lon.append(f_lon[burning[ok]])
                day.append(d[ok])
                night.append(np.full(ok.sum(), is_night))
                p.append(np.full(ok.sum(), prob))
                fid.append(burning[ok])
    if not lat:   # the period holds no stubble season
        return _empty()
    return _observe(world, np.concatenate(lat), np.concatenate(lon), np.concatenate(day),
                    np.concatenate(night), np.concatenate(p), 8.0, 0.7, "field",
                    np.concatenate(fid))


def _forest(world: _World) -> pd.DataFrame:
    """Fires that spread across pixels for days, then stop."""
    rng = world.rng
    la0, la1, lo0, lo1 = FOREST_BOX
    lat, lon, day, night, fid = [], [], [], [], []
    fire_id = 0
    for year in sorted(set(world.dates.year)):
        span = _season_days(world, year, (2, 1), (5, 31))
        if span is None:
            continue
        for _ in range(N_FOREST_FIRES):
            start = int(rng.integers(span[0], span[1] + 1))
            length = int(rng.integers(2, 9))
            heading = rng.uniform(0, 2 * math.pi)
            speed = rng.uniform(400, 1200)
            f_lat, f_lon = rng.uniform(la0, la1), rng.uniform(lo0, lo1)
            for k in range(length):
                d = start + k
                if d >= world.n_days:
                    break
                turn = heading + rng.normal(0, 0.35)
                c_lat, c_lon = offset(f_lat, f_lon, k * speed * math.cos(turn),
                                      k * speed * math.sin(turn))
                pixels = int(rng.integers(1, 5))
                dx, dy = rng.normal(0, 300, (2, pixels))
                p_lat, p_lon = offset(np.full(pixels, c_lat), np.full(pixels, c_lon), dx, dy)
                for is_night in (False, True):
                    lat.append(p_lat)
                    lon.append(p_lon)
                    day.append(np.full(pixels, d))
                    night.append(np.full(pixels, is_night))
                    fid.append(np.full(pixels, fire_id))
            fire_id += 1
    if not lat:   # the period holds no forest-fire season
        return _empty()
    night_arr = np.concatenate(night)
    return _observe(world, np.concatenate(lat), np.concatenate(lon), np.concatenate(day),
                    night_arr, np.where(night_arr, 0.35, 0.60), 15.0, 0.8, "forest",
                    np.concatenate(fid))


# -------------------------------------------------------------------------- events

def _event_rows(world: _World, lat: float, lon: float, day: int,
                passes: list[tuple[str, bool, float]], kind: str, origin: str) -> pd.DataFrame:
    """Deterministic detections for a scripted event: (sensor, night, frp) per pass."""
    rows = []
    for code, night, frp in passes:
        s = _BY_CODE[code]
        hour = s.night_utc if night else s.day_utc
        rows.append({"sensor": code,
                     "acq_datetime": world.dates[day] + pd.Timedelta(minutes=round(hour * 60)),
                     "day": day, "night": night, "latitude": lat, "longitude": lon,
                     "frp": frp, "origin_kind": kind, "origin_id": origin})
        world.force_clear(int(era5_cell(lat, lon)), day, night)
    return pd.DataFrame(rows)


def _events(world: _World, base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Script the events the later stages are tested against.

    Dates are anchored to the period's end, so the default period reproduces the
    real calendar: the blowout runs 9 June to 15 November, like Baghjan in 2020.
    """
    last = world.n_days - 1
    rows, truth = [base], []

    def drop(source: str, day: int, night: bool) -> None:
        # Every sensor, not just VIIRS: a normal MODIS pass between the two spike
        # passes would make "two consecutive passes" untrue in the ground truth.
        mask = ((rows[0]["origin_id"] == source) & (rows[0]["day"] == day)
                & (rows[0]["night"] == night))
        rows[0] = rows[0].loc[~mask]

    def at(days_before_end: int) -> int | None:
        d = last - days_before_end
        return d if d >= 0 else None

    steel, flare, power = _SPEC["steel_works"], _SPEC["flare_jamnagar"], _SPEC["power_plant"]

    d = at(132)   # a fire at a running steel works, seen on two consecutive passes
    if d is not None:
        drop("steel_works", d, False)
        frp = 8 * steel.frp_median
        rows.append(_event_rows(world, steel.lat, steel.lon, d,
                                [("N20", False, frp), ("N", False, frp)], "event", "E1"))
        truth.append(("E1", "furnace_fire", "steel_works", steel.lat, steel.lon, d, d, 2, 8.0,
                      "C", "confirmed", "two consecutive passes at 8x the median"))

    d = at(87)    # a blast at a flare: one extreme pass, and the next pass is normal
    if d is not None:
        drop("flare_jamnagar", d, True)
        rows.append(_event_rows(world, flare.lat, flare.lon, d,
                                [("N20", True, 25 * flare.frp_median)], "event", "E2"))
        rows.append(_event_rows(world, flare.lat, flare.lon, d,
                                [("N", True, flare.frp_median)], "source", "flare_jamnagar"))
        truth.append(("E2", "flare_blast", "flare_jamnagar", flare.lat, flare.lon, d, d, 1,
                      25.0, "C", "provisional", "one extreme pass; the next is normal"))

    d = at(320)   # a warehouse fire 400 m outside the power plant's fence: new ignition
    if d is not None:
        half = power.spread_m + 600
        w_lat, w_lon = offset(power.lat, power.lon, half + 400, 0.0)
        rows.append(_event_rows(world, float(w_lat), float(w_lon), d,
                                [("N20", False, 30.0), ("N", False, 22.0), ("Aqua", False, 35.0)],
                                "event", "E3"))
        truth.append(("E3", "warehouse_fire", None, float(w_lat), float(w_lon), d, d, 3, None,
                      "A", None, "new ignition just outside an industrial polygon"))

    for name, event_id, kind, road, note in (
            ("blowout", "E4", "blowout", "A",
             "five-month accident: must never be routed to Road B while it burns"),
            ("new_flare", "E5", "new_flare", "A",
             "new infrastructure: Road A, then a provisional source that keeps alerting")):
        spec = _SPEC[name]
        first, stop = world.window(spec.active)
        truth.append((event_id, kind, name, spec.lat, spec.lon, first, stop, None, None,
                      road, None, note))

    events = pd.DataFrame(truth, columns=[
        "event_id", "kind", "source", "latitude", "longitude", "start_day", "end_day",
        "passes", "multiplier", "expected_road", "expected_tier", "note"])
    events["start"] = world.dates[events["start_day"].to_numpy()].date
    events["end"] = world.dates[events["end_day"].to_numpy()].date
    events = events.drop(columns=["start_day", "end_day"])
    return pd.concat(rows, ignore_index=True), events


# ------------------------------------------------------------------ FIRMS shaping

def _radiometry(base: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """The FIRMS columns that follow from FRP. Computed once per detection, so an
    NRT copy and its SP original agree on everything but the reprocessing shift."""
    out = base.copy()
    n = len(out)
    log_frp = np.log1p(out["frp"].to_numpy())
    viirs = out["sensor"].isin(["N", "N20"]).to_numpy()
    out["bt4"] = np.where(viirs, np.clip(300 + 12 * log_frp + rng.normal(0, 3, n), 295, 367),
                          np.clip(310 + 15 * log_frp + rng.normal(0, 4, n), 300, 450))
    out["bt5"] = np.where(viirs, 290 + 2 * log_frp + rng.normal(0, 2, n),
                          295 + 3 * log_frp + rng.normal(0, 3, n))
    out["scan"] = np.where(viirs, rng.uniform(0.39, 0.75, n), rng.uniform(1.0, 4.8, n))
    out["track"] = np.where(viirs, rng.uniform(0.36, 0.70, n), rng.uniform(1.0, 2.0, n))
    high = (out["bt4"] >= 360) | (out["frp"] >= 30)
    low = rng.random(n) < 0.08
    viirs_conf = np.where(high, "h", np.where(low, "l", "n"))
    modis_conf = np.clip(np.round(rng.normal(60 + 10 * log_frp, 12)), 0, 100).astype(int)
    out["confidence"] = np.where(viirs, viirs_conf, modis_conf.astype(str))
    return out


def _firms_rows(frame: pd.DataFrame, product: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Shape detections as FIRMS CSV rows: (VIIRS rows, MODIS rows)."""
    out = []
    for instrument, columns, decimals in (("VIIRS", VIIRS_COLUMNS, 5),
                                          ("MODIS", MODIS_COLUMNS, 4)):
        sub = frame[frame["sensor"].map(lambda c: _BY_CODE[c].instrument) == instrument]
        dt = sub["acq_datetime"]
        rows = pd.DataFrame({
            "latitude": sub["latitude"].round(decimals),
            "longitude": sub["longitude"].round(decimals),
            columns[2]: sub["bt4"].round(2),
            "scan": sub["scan"].round(2), "track": sub["track"].round(2),
            "acq_date": dt.dt.strftime("%Y-%m-%d"), "acq_time": dt.dt.strftime("%H%M"),
            "satellite": sub["sensor"].map(
                lambda c: _BY_CODE[c].nrt_code if product == "NRT" else c),
            "instrument": instrument,
            "confidence": sub["confidence"].astype(int) if instrument == "MODIS"
            else sub["confidence"],
            "version": {("VIIRS", "SP"): "2", ("VIIRS", "NRT"): "2.0NRT",
                        ("MODIS", "SP"): "61.03", ("MODIS", "NRT"): "6.1NRT"}[
                (instrument, product)],
            columns[11]: sub["bt5"].round(2),
            "frp": sub["frp"].round(2),
            "daynight": np.where(sub["night"], "N", "D"),
        })
        if product == "SP":
            # FIRMS marks recurring sites "static land source" (2) in the archive.
            rows["type"] = np.where(sub["firms_static"], 2, 0)
            rows = rows[columns]
        else:
            rows = rows[[c for c in columns if c != "type"]]   # NRT has no type
        out.append(rows.sort_values(["acq_date", "acq_time"], kind="stable")
                   .reset_index(drop=True))
    return out[0], out[1]


def _products(world: _World, base: pd.DataFrame) -> dict[str, pd.DataFrame]:
    rng = world.rng
    last_date = world.dates[-1]
    sp_end = last_date - pd.Timedelta(days=SP_LAG_DAYS)
    nrt_start = last_date - pd.Timedelta(days=NRT_DAYS - 1)
    base = base.copy()
    base["firms_static"] = (base["origin_kind"] == "source") & base["origin_id"].map(
        lambda o: o in _SPEC and _SPEC[o].active is None)

    files: dict[str, pd.DataFrame] = {}
    sp = base[base["acq_datetime"].dt.floor("D") <= sp_end]
    for year in sorted(sp["acq_datetime"].dt.year.unique()):
        viirs, modis = _firms_rows(sp[sp["acq_datetime"].dt.year == year], "SP")
        for code, slug in (("N", "viirs-snpp"), ("N20", "viirs-jpss1")):
            part = viirs[viirs["satellite"] == code].reset_index(drop=True)
            files[f"firms/{slug}_{year}_India.csv"] = part
        files[f"firms/modis_{year}_India.csv"] = modis

    nrt = base[base["acq_datetime"].dt.floor("D") >= nrt_start].copy()
    shift = rng.normal(0, NRT_SHIFT_DEG, (2, len(nrt)))
    nrt["latitude"] += shift[0]
    nrt["longitude"] += shift[1]
    nrt["frp"] *= rng.uniform(0.95, 1.05, len(nrt))
    viirs, modis = _firms_rows(nrt, "NRT")
    files["firms_nrt/VIIRS_SNPP_NRT.csv"] = viirs[viirs["satellite"] == "N"].reset_index(
        drop=True)
    files["firms_nrt/VIIRS_NOAA20_NRT.csv"] = viirs[viirs["satellite"] == "N20"].reset_index(
        drop=True)
    files["firms_nrt/MODIS_NRT.csv"] = modis
    return files


def _vnf(world: _World, base: pd.DataFrame) -> pd.DataFrame:
    """VNF-shaped temperatures: night VIIRS only, and only where a fit succeeds."""
    rng = world.rng
    night = base[base["night"] & base["sensor"].isin(["N", "N20"])]
    fit = np.zeros(len(night))
    temp = np.full(len(night), 950.0)
    temp_sd = np.full(len(night), 100.0)
    area = np.full(len(night), 8000.0)
    kinds = night["origin_kind"].to_numpy()
    origins = night["origin_id"].to_numpy()
    for i, (kind, origin) in enumerate(zip(kinds, origins, strict=True)):
        if kind == "source":
            spec = _SPEC[origin]
            fit[i], temp[i], temp_sd[i], area[i] = (spec.vnf_fit, spec.temp_k, spec.temp_sd,
                                                    spec.area_m2)
        elif kind == "event":
            fit[i], temp[i], temp_sd[i], area[i] = 0.9, 1800.0, 80.0, 60.0
        elif kind == "forest":
            fit[i], temp[i], area[i] = 0.10, 1000.0, 6000.0
        else:   # stubble: cool and diffuse, so the Planck fit usually fails
            fit[i] = 0.05
    ok = rng.random(len(night)) < fit
    sel = night[ok]
    n = len(sel)
    dx, dy = rng.normal(0, 200, (2, n))
    lat, lon = offset(sel["latitude"].to_numpy(), sel["longitude"].to_numpy(), dx, dy)
    # Index + Index adds by position; Series + Index could align by label instead.
    when = pd.DatetimeIndex(sel["acq_datetime"]) + pd.to_timedelta(
        rng.normal(0, 30, n).round(), unit="s")
    return pd.DataFrame({
        "Date_Mscan": when.strftime("%Y/%m/%d %H:%M:%S.000"),
        "Lat_GMTCO": np.round(lat, 5), "Lon_GMTCO": np.round(lon, 5),
        "Temp_BB": np.round(rng.normal(temp[ok], temp_sd[ok]), 1),
        "Area_BB": np.round(area[ok] * np.exp(rng.normal(0, 0.3, n)), 2),
        "RH": np.round(sel["frp"].to_numpy() * rng.uniform(0.8, 1.2, n), 3),
        "Cloud_Mask": 0,
        "Sat": np.where(sel["sensor"] == "N", "npp", "j01"),
    })[VNF_COLUMNS].reset_index(drop=True)


# ----------------------------------------------------------------------- truth

def _square_wkt(lat: float, lon: float, half_m: float) -> str:
    lats, lons = offset(np.full(4, lat), np.full(4, lon),
                        np.array([-1, 1, 1, -1]) * half_m, np.array([-1, -1, 1, 1]) * half_m)
    ring = [*zip(lons, lats, strict=True), (lons[0], lats[0])]
    return "POLYGON((" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in ring) + "))"


def _box_wkt(box: tuple[float, float, float, float]) -> str:
    la0, la1, lo0, lo1 = box
    return (f"POLYGON(({lo0} {la0}, {lo1} {la0}, {lo1} {la1}, {lo0} {la1}, "
            f"{lo0} {la0}))")


def _truth(world: _World, base: pd.DataFrame, events: pd.DataFrame) -> dict[str, pd.DataFrame]:
    sources = []
    facilities = []
    for i, spec in enumerate(SOURCES, start=1):
        first, stop = world.window(spec.active)
        sources.append({"source_id": i, "name": spec.name, "cls": spec.cls,
                        "latitude": spec.lat, "longitude": spec.lon, "spots": spec.spots,
                        "spread_m": spec.spread_m, "active_from": world.dates[first].date(),
                        "active_to": world.dates[stop].date(), "expected": spec.expected})
        geom = (f"POINT({spec.lon} {spec.lat})"
                if spec.facility_tag.startswith("man_made=petroleum_well")
                else _square_wkt(spec.lat, spec.lon, spec.spread_m + 600))
        facilities.append({"facility_id": i, "name": spec.name, "tag": spec.facility_tag,
                           "wkt": geom})
    detections = base[["sensor", "acq_datetime", "latitude", "longitude", "frp",
                       "night", "origin_kind", "origin_id"]].copy()
    detections["daynight"] = np.where(detections.pop("night"), "N", "D")
    return {
        "sources": pd.DataFrame(sources),
        "events": events,
        "facilities": pd.DataFrame(facilities),
        "landcover": pd.DataFrame([
            {"worldcover_class": 40, "name": "cropland", "wkt": _box_wkt(PADDY_BOX)},
            {"worldcover_class": 10, "name": "tree cover", "wkt": _box_wkt(FOREST_BOX)}]),
        "detections": detections.sort_values("acq_datetime", kind="stable")
        .reset_index(drop=True),
    }


# ------------------------------------------------------------------------ public

def generate(start: date = DEFAULT_START, end: date = DEFAULT_END, seed: int = 7) -> Fixture:
    """Build the fixture world. Same arguments, same world, byte for byte."""
    if end <= start:
        raise ValueError(f"end {end} must be after start {start}")
    world = _World(start, end, seed)
    # Empty parts are dropped: pandas is changing how they affect concat dtypes.
    parts = [f for f in (_persistent(world), _paddy(world), _forest(world)) if len(f)]
    base = pd.concat(parts, ignore_index=True)
    base, events = _events(world, base)
    base = base.sort_values(["acq_datetime", "sensor"], kind="stable").reset_index(drop=True)
    base = _radiometry(base, world.rng)
    return Fixture(start=start, end=end, seed=seed, firms=_products(world, base),
                   vnf=_vnf(world, base), cloud=world.cloud_table(),
                   truth=_truth(world, base, events))


def inject_spikes(history: pd.DataFrame, n_events: int, *,
                  multiplier: tuple[float, float] = (6.0, 12.0), passes: int = 2,
                  holdout: float = 0.5, seed: int | np.random.Generator = 0,
                  time_col: str = "acq_datetime",
                  frp_col: str = "frp") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Add excursions of known size and time to one source's detection history.

    The earliest ``holdout`` share of the observations is left untouched, so a
    baseline built from it is clean. Each event lands on ``passes`` consecutive real
    observations -- the source's own cadence, cloud gaps included -- and lifts their
    FRP to ``m`` times the untouched baseline median, never lowering it. Events are
    separated by at least one untouched observation, so two never merge.

    Returns ``(spiked, truth)``. ``spiked`` is a copy with the same index; ``truth``
    has one row per event, with the index labels it changed.
    """
    if passes < 1 or n_events < 1:
        raise ValueError("n_events and passes must be at least 1")
    if not history.index.is_unique:
        raise ValueError("history index must be unique; the truth refers to rows by label")
    rng = np.random.default_rng(seed) if not isinstance(seed, np.random.Generator) else seed
    order = history.sort_values(time_col, kind="stable").index
    n = len(order)
    cut = int(math.ceil(n * holdout))
    baseline = float(history.loc[order[:cut], frp_col].median()) if cut else math.nan
    slots = (n - cut) // (passes + 1)
    if not cut or math.isnan(baseline) or slots < n_events:
        raise ValueError(
            f"history too short: {n} observations leave room for {max(slots, 0)} events "
            f"of {passes} passes after a {holdout:.0%} holdout; asked for {n_events}")

    starts = np.sort(rng.choice(slots, n_events, replace=False)) * (passes + 1) + cut
    factors = rng.uniform(*multiplier, n_events)
    spiked = history.copy()
    records = []
    for i, (start, factor) in enumerate(zip(starts, factors, strict=True)):
        labels = list(order[start:start + passes])
        target = baseline * factor
        spiked.loc[labels, frp_col] = np.maximum(spiked.loc[labels, frp_col], target)
        times = history.loc[labels, time_col]
        records.append({"event": i, "start": times.min(), "end": times.max(),
                        "multiplier": float(factor), "baseline_median": baseline,
                        "frp": target, "passes": passes, "rows": labels})
    return spiked, pd.DataFrame(records)


def summarise(fx: Fixture) -> dict:
    """Headline counts, for the CLI and for sanity checks."""
    det = fx.truth["detections"]
    return {
        "period": f"{fx.start} to {fx.end}",
        "detections": int(len(det)),
        "by_origin": {k: int(v) for k, v in det["origin_kind"].value_counts().items()},
        "night_share": round(float((det["daynight"] == "N").mean()), 3),
        "vnf_rows": int(len(fx.vnf)),
        "vnf_share": round(len(fx.vnf) / max(len(det), 1), 3),
        "files": {k: int(len(v)) for k, v in fx.firms.items()},
        "events": fx.truth["events"]["kind"].tolist(),
    }
