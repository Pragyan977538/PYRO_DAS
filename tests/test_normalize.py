"""FIRMS normalisation and SP/NRT de-duplication.

Pure pandas, no database. The last test runs against the real 2023 archive file
when it has been downloaded (``make spike`` fetches it) and skips otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from firewatch.ingest.normalize import COLUMNS, dedupe_sp_nrt, normalise_firms

REPO = Path(__file__).resolve().parent.parent

VIIRS_ROW = {
    "latitude": 22.3, "longitude": 69.9, "bright_ti4": 340.1, "scan": 0.4,
    "track": 0.5, "acq_date": "2023-01-01", "acq_time": "2045", "satellite": "N",
    "instrument": "VIIRS", "confidence": "n", "version": "2", "bright_ti5": 290.2,
    "frp": 12.5, "daynight": "N", "type": 2,
}
MODIS_ROW = {
    "latitude": 30.2, "longitude": 75.0, "brightness": 318.0, "scan": 1.1,
    "track": 1.0, "acq_date": "2023-01-01", "acq_time": 821, "satellite": "Aqua",
    "instrument": "MODIS", "confidence": 62, "version": "61.03", "bright_t31": 305.0,
    "frp": 7.6, "daynight": "D", "type": 0,
}


def _frame(row: dict, n: int = 1, **overrides) -> pd.DataFrame:
    return pd.DataFrame([{**row, **overrides}] * n)


def test_viirs_and_modis_normalise_to_the_same_columns():
    viirs = normalise_firms(_frame(VIIRS_ROW))
    modis = normalise_firms(_frame(MODIS_ROW))
    assert list(viirs.columns) == list(modis.columns) == COLUMNS
    assert viirs.loc[0, "bt4"] == pytest.approx(340.1)
    assert modis.loc[0, "bt4"] == pytest.approx(318.0)
    assert modis.loc[0, "bt5"] == pytest.approx(305.0)


def test_frame_mixing_instruments_is_rejected():
    mixed = pd.concat([_frame(VIIRS_ROW), _frame(MODIS_ROW)], ignore_index=True)
    with pytest.raises(ValueError, match="mixes MODIS and VIIRS"):
        normalise_firms(mixed)


def test_acq_time_is_zero_padded_and_utc():
    out = normalise_firms(_frame(MODIS_ROW))  # acq_time 821 as an int
    assert out.loc[0, "acq_datetime"] == pd.Timestamp("2023-01-01 08:21", tz="UTC")


@pytest.mark.parametrize(("row", "sat", "sensor"), [
    (VIIRS_ROW, "N", "VIIRS_SNPP"),
    (VIIRS_ROW, "N20", "VIIRS_NOAA20"),
    (VIIRS_ROW, "N21", "VIIRS_NOAA21"),
    (MODIS_ROW, "Aqua", "MODIS_AQUA"),
    (MODIS_ROW, "T", "MODIS_TERRA"),
])
def test_sensor_codes(row, sat, sensor):
    out = normalise_firms(_frame(row, satellite=sat))
    assert out.loc[0, "sensor"] == sensor
    assert out.loc[0, "instrument"] == row["instrument"]


def test_unknown_satellite_fails_loudly():
    with pytest.raises(ValueError, match="unknown FIRMS"):
        normalise_firms(_frame(VIIRS_ROW, satellite="X9"))


def test_instrument_inferred_when_column_absent():
    raw = _frame(MODIS_ROW, satellite="A").drop(columns="instrument")
    assert normalise_firms(raw).loc[0, "sensor"] == "MODIS_AQUA"


@pytest.mark.parametrize(("version", "product"), [
    ("2", "SP"), ("2.0NRT", "NRT"), ("61.03", "SP"), ("6.1NRT", "NRT"),
])
def test_product_from_version(version, product):
    assert normalise_firms(_frame(VIIRS_ROW, version=version)).loc[0, "product"] == product


def test_blank_version_fails_loudly():
    with pytest.raises(ValueError, match="version"):
        normalise_firms(_frame(VIIRS_ROW, version=None))


def test_firms_type_is_null_not_zero_when_absent():
    """NRT has no FIRMS type; 0 would falsely claim 'vegetation'."""
    out = normalise_firms(_frame(VIIRS_ROW, version="2.0NRT").drop(columns="type"))
    assert out["firms_type"].isna().all()


def test_dedupe_drops_nrt_only_where_sp_covers_the_sensor_day():
    rows = [
        # day 1: SP and NRT for S-NPP -> NRT goes (reprocessing moved it slightly)
        {**VIIRS_ROW},
        {**VIIRS_ROW, "version": "2.0NRT", "latitude": 22.3004},
        # day 1: NRT for NOAA-20 -> kept, SP only covers S-NPP
        {**VIIRS_ROW, "version": "2.0NRT", "satellite": "N20"},
        # day 2: NRT only -> kept
        {**VIIRS_ROW, "version": "2.0NRT", "acq_date": "2023-01-02"},
    ]
    out = dedupe_sp_nrt(normalise_firms(pd.DataFrame(rows)))
    got = sorted(zip(out["sensor"], out["product"], out["acq_datetime"].dt.day,
                     strict=True))
    assert got == [("VIIRS_NOAA20", "NRT", 1), ("VIIRS_SNPP", "NRT", 2),
                   ("VIIRS_SNPP", "SP", 1)]


def test_dedupe_drops_exact_repeats():
    """Overlapping API windows return the same pixel twice."""
    out = dedupe_sp_nrt(normalise_firms(_frame(VIIRS_ROW, n=3)))
    assert len(out) == 1


ARCHIVE = REPO / "data" / "raw" / "firms" / "viirs-snpp_2023_India.csv"


@pytest.mark.skipif(not ARCHIVE.exists(), reason="real archive not downloaded")
def test_real_archive_sample_normalises():
    raw = pd.read_csv(ARCHIVE, nrows=20_000, dtype={"acq_time": str})
    out = dedupe_sp_nrt(normalise_firms(raw))
    assert set(out["sensor"]) == {"VIIRS_SNPP"}
    assert set(out["product"]) == {"SP"}
    assert set(out["firms_type"].dropna().unique()) <= {0, 1, 2, 3}
    assert out["acq_datetime"].dt.year.eq(2023).all()
