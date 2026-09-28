"""FIRMS column normalisation and SP/NRT de-duplication.

Two problems corrupt every downstream number if they get past ingestion, so both
are fixed here, at the moment data enters the system, and nowhere else:

* MODIS and VIIRS name the same quantities differently (``brightness`` vs
  ``bright_ti4``). A join written against one name silently drops the other
  instrument's rows.
* The archive (SP, science-quality) and near-real-time (NRT) products overlap for
  recent months, and reprocessing shifts positions slightly, so the natural key
  does not see the duplicate. A pixel counted twice inflates persistence and every
  baseline built on it.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from firewatch.db import get_conn

#: FIRMS column -> FireWatch column. Both instruments end up with bt4 / bt5.
RENAME: dict[str, str] = {
    "brightness": "bt4",
    "bright_ti4": "bt4",
    "bright_t31": "bt5",
    "bright_ti5": "bt5",
    "type": "firms_type",
}

#: (instrument, FIRMS satellite code) -> canonical sensor. The archive and the API
#: spell MODIS satellites differently, so both spellings are listed. An unknown
#: code raises rather than guessing: a mislabelled sensor corrupts its baselines.
SENSORS: dict[tuple[str, str], str] = {
    ("VIIRS", "N"): "VIIRS_SNPP",
    ("VIIRS", "N20"): "VIIRS_NOAA20",
    ("VIIRS", "N21"): "VIIRS_NOAA21",
    ("MODIS", "Terra"): "MODIS_TERRA",
    ("MODIS", "T"): "MODIS_TERRA",
    ("MODIS", "Aqua"): "MODIS_AQUA",
    ("MODIS", "A"): "MODIS_AQUA",
}

#: Columns every normalised frame carries, in order.
COLUMNS: list[str] = [
    "acq_datetime", "latitude", "longitude", "sensor", "instrument", "satellite",
    "product", "version", "daynight", "frp", "bt4", "bt5", "scan", "track",
    "confidence", "firms_type",
]

#: Mirrors the detections_natural_key index in sql/001_schema.sql.
NATURAL_KEY: list[str] = ["sensor", "acq_datetime", "latitude", "longitude"]

_FLOATS = ["latitude", "longitude", "frp", "bt4", "bt5", "scan", "track"]


def normalise_firms(raw: pd.DataFrame) -> pd.DataFrame:
    """Return one FIRMS file's rows in FireWatch column names and types.

    Normalise each file on its own. A frame that already mixes MODIS and VIIRS
    column names is rejected: renaming it would create two ``bt4`` columns, which
    is exactly the silent row loss this module exists to prevent.
    """
    has_modis = "brightness" in raw.columns
    has_viirs = "bright_ti4" in raw.columns
    if has_modis and has_viirs:
        raise ValueError(
            "frame mixes MODIS and VIIRS column names; normalise each file separately"
        )
    if not (has_modis or has_viirs):
        raise ValueError("no brightness column found; is this a FIRMS file?")
    if "version" not in raw.columns:
        raise ValueError("FIRMS 'version' column missing; SP vs NRT cannot be told apart")

    df = raw.rename(columns=RENAME)

    # The archive carries an instrument column; some API products do not, but the
    # brightness column names identify the instrument unambiguously.
    if "instrument" in df.columns:
        df["instrument"] = df["instrument"].astype(str).str.strip().str.upper()
    else:
        df["instrument"] = "MODIS" if has_modis else "VIIRS"

    df["satellite"] = df["satellite"].astype(str).str.strip()
    pairs = list(zip(df["instrument"], df["satellite"], strict=True))
    unknown = sorted({p for p in pairs if p not in SENSORS})
    if unknown:
        raise ValueError(f"unknown FIRMS instrument/satellite codes: {unknown}")
    df["sensor"] = [SENSORS[p] for p in pairs]

    hhmm = df["acq_time"].astype(str).str.strip().str.zfill(4)
    df["acq_datetime"] = pd.to_datetime(
        df["acq_date"].astype(str).str.strip() + " " + hhmm,
        format="%Y-%m-%d %H%M",
        utc=True,
    )

    # A blank version would silently default to SP and win every dedupe.
    version = df["version"].astype("string").str.strip()
    if (version.isna() | (version == "")).any():
        raise ValueError("rows with no FIRMS version; SP vs NRT cannot be told apart")
    df["version"] = version.astype(str)
    df["product"] = df["version"].str.contains("NRT", case=False).map(
        {True: "NRT", False: "SP"}
    )

    for col in _FLOATS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df["daynight"] = df["daynight"].astype(str).str.strip().str.upper()
    df["confidence"] = df["confidence"].astype(str).str.strip()
    # Archive only: NRT rows genuinely have no FIRMS type, so NULL, not 0.
    if "firms_type" in df.columns:
        df["firms_type"] = pd.to_numeric(df["firms_type"], errors="coerce").astype("Int16")
    else:
        df["firms_type"] = pd.array([pd.NA] * len(df), dtype="Int16")

    return df[COLUMNS].reset_index(drop=True)


def dedupe_sp_nrt(df: pd.DataFrame) -> pd.DataFrame:
    """Drop NRT rows for every sensor-day that SP also covers, then exact repeats.

    SP is the reprocessed product and wins wherever both exist. Matching by
    sensor-day rather than by position is deliberate: reprocessing moves pixel
    centres slightly, so the same observation would otherwise survive twice.
    """
    day = df["acq_datetime"].dt.strftime("%Y-%m-%d")
    key = df["sensor"].astype(str) + "|" + day
    covered = set(key[df["product"] == "SP"])
    superseded = (df["product"] == "NRT") & key.isin(covered)
    out = df.loc[~superseded]
    return out.drop_duplicates(subset=NATURAL_KEY, keep="first").reset_index(drop=True)


SUPERSEDE_NRT_SQL = """
DELETE FROM detections
 WHERE product = 'NRT'
   AND sensor = %(sensor)s
   AND acq_datetime >= %(start)s
   AND acq_datetime <  %(end)s
"""


def supersede_nrt(sensor: str, start: datetime, end: datetime) -> int:
    """Delete NRT rows that an SP load for ``[start, end)`` now covers.

    The database-side twin of :func:`dedupe_sp_nrt`: call it after loading SP data
    for a sensor and date range, because the live NRT rows for those days were
    loaded weeks earlier. Returns the number of rows deleted.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(SUPERSEDE_NRT_SQL, {"sensor": sensor, "start": start, "end": end})
        return cur.rowcount
