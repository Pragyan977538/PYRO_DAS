"""Stage 4: weak labels and Model 1.

The leakage guard gets the most tests, because it is the rule that makes every
other number in this stage mean something.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import fresh_database, postgres_reachable

from firewatch.grid import to_metres
from firewatch.ingest.fixture import SOURCES
from firewatch.models import labels as lb
from firewatch.models import source_clf as clf
from firewatch.registry.fingerprint import FEATURES

# --------------------------------------------------------------- leakage guard


def test_feats_pass_the_guard():
    clf.check_leakage(clf.FEATS, lb.barred_inputs())


@pytest.mark.parametrize("leak", ["landcover", "osm_distance", "lat", "firms_type",
                                  "gppd_distance", "dist_industrial"])
def test_guard_fails_loudly_on_a_label_input(leak):
    with pytest.raises(clf.LeakageError, match=leak):
        clf.check_leakage([*clf.FEATS, leak], lb.barred_inputs())


def test_every_label_input_is_barred():
    for inputs in lb.LABEL_INPUTS.values():
        assert inputs <= lb.barred_inputs()


def test_every_fingerprint_feature_is_used_or_excluded_with_a_reason():
    assert set(clf.FEATS) <= set(FEATURES)
    assert set(FEATURES) == set(clf.FEATS) | set(clf.EXCLUDED)
    assert not set(clf.FEATS) & set(clf.EXCLUDED)


def test_no_absolute_brightness_temperature():
    """Absolute I4/I5 temperatures carry the background climate: location."""
    assert not [f for f in clf.FEATS if f.startswith(("bt4_", "bt5_"))]


# ------------------------------------------------------------------- labels

def _near(rows):
    return pd.DataFrame(rows, columns=["source_id", "grp", "evidence", "osm_ref", "name", "d"])


def test_label_priority_most_specific_first():
    near = _near([
        (1, "industrial_other", "osm:w1", 1, "estate", 100.0),
        (1, "mining", "osm:w2", 2, "colliery", 900.0),        # mining beats generic
        (2, "mining", "osm:w3", 3, "quarry", 50.0),
        (2, "oil_gas", "osm:n4", 4, "refinery", 950.0),        # oil_gas beats all
        (3, "thermal_power", "gppd:IND1", None, "NTPC", 400.0),
        (3, "industrial_other", "osm:w5", 5, None, 10.0),
        (4, "kiln", "osm:n6", 6, "brickworks", 30.0),          # kiln: no label
        (4, "industrial_other", "osm:w7", 7, None, 300.0),
    ])
    out = lb.assign(pd.Index([1, 2, 3, 4, 5]), near)
    assert out.loc[1, "label"] == "mining" and out.loc[2, "label"] == "oil_gas"
    assert out.loc[3, "label"] == "heavy_industry" and out.loc[3, "evidence"] == "gppd:IND1"
    assert out.loc[4, "label"] is None and out.loc[4, "label_group"] == "kiln"
    assert out.loc[5, "label"] is None and out.loc[5, "label_group"] is None
    assert out.loc[2, "groups_near"] == {"mining": 50, "oil_gas": 950}


@pytest.mark.parametrize(("n", "industrial", "added"), [
    (42, 34, False),     # the real first build: mostly confirmed industry
    (42, 10, True),
    (20, 0, False),      # too few
])
def test_biomass_needs_numbers_and_honesty(n, industrial, added):
    candidates = pd.Index(range(n))
    decision = lb.biomass_decision(candidates, set(range(industrial)))
    assert decision["class_added"] is added


def test_biomass_candidates_skip_kiln_and_labelled():
    labels = lb.assign(pd.Index([1, 2, 3]), _near([(2, "kiln", "osm:n1", 1, None, 5.0),
                                                    (3, "mining", "osm:w2", 2, None, 5.0)]))
    cover = pd.Series({1: 40, 2: 40, 3: 10})
    assert list(lb.biomass_candidates(labels, cover)) == [1]


# ------------------------------------------------------------------ Model 1

def test_spatial_blocks():
    b = clf.spatial_blocks([22.1, 23.9, 24.1], [69.5, 69.9, 69.9])
    assert b[0] == b[1] != b[2]


def _separable(n=240, seed=0):
    rng = np.random.default_rng(seed)
    y = np.repeat([0, 1, 2], n // 3)
    X = pd.DataFrame({"a": y + rng.normal(0, 0.3, n), "b": rng.normal(0, 1, n),
                      "c": np.where(rng.random(n) < 0.3, np.nan, y * 2.0)})
    groups = rng.integers(0, 12, n)
    return X, y, groups


def test_cross_validation_learns_a_real_signal_and_not_noise():
    X, y, groups = _separable()
    proba, fold = clf.cross_validate(X, y, groups, 3)
    assert (fold >= 0).all() and np.allclose(proba.sum(axis=1), 1)
    assert clf.scores(y, proba.argmax(axis=1), ["a", "b", "c"])["accuracy"] > 0.9
    shuffled = np.random.default_rng(1).permutation(y)
    noise, _ = clf.cross_validate(X[["b"]], shuffled, groups, 3)
    assert clf.scores(shuffled, noise.argmax(axis=1), ["a", "b", "c"])["accuracy"] < 0.5


def test_groups_never_straddle_folds():
    from sklearn.model_selection import GroupKFold

    X, y, groups = _separable()
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups):
        assert not set(groups[tr]) & set(groups[te])


def test_scores_shape_and_usable_features():
    s = clf.scores(np.array([0, 1, 2, 2]), np.array([0, 2, 2, 2]), ["x", "y", "z"])
    assert s["confusion"]["matrix"] == [[1, 0, 0], [0, 0, 1], [0, 0, 2]]
    assert s["per_class"]["z"]["recall"] == 1.0
    keep, dropped = clf.usable(pd.DataFrame({"ok": [1.0, 2.0], "vnf": [np.nan, np.nan],
                                             "flat": [0.0, 0.0]}))
    assert keep == ["ok"] and set(dropped) == {"vnf", "flat"}


def test_model_round_trip(tmp_path):
    X, y, _ = _separable()
    model = clf.fit(X, y, ["a", "b", "c"])
    model.save(tmp_path, {"note": "test"})
    again = clf.Model1.load(tmp_path)
    assert again.classes == ["a", "b", "c"] and again.features == ["a", "b", "c"]
    np.testing.assert_array_equal(model.predict(X)[0], again.predict(X)[0])
    importance = clf.shap_importance(model.model, X, ["a", "b", "c"])
    assert next(iter(importance["overall"])) == "a"          # the informative one


# ------------------------------------------------------------ labels on the DB

needs_db = pytest.mark.skipif(not postgres_reachable(), reason="no PostgreSQL reachable")


@pytest.fixture(scope="module")
def labelled_db(tmp_path_factory):
    from firewatch.ingest.firms_archive import load_archive
    from firewatch.ingest.observability import load_observability
    from firewatch.ingest.osm import load_osm
    from firewatch.registry.build import build, read_detections, write
    from firewatch.registry.cells import Gate

    for data_dir in fresh_database("firewatch_test_models", tmp_path_factory):
        load_archive([2022, 2023])
        load_observability([2022, 2023])
        load_osm()
        write(build(read_detections("VIIRS"), read_detections("MODIS"),
                    Gate(months=3, days=10, years=2)))
        yield data_dir


@needs_db
def test_fixture_sources_get_their_true_class(labelled_db):
    from firewatch.db import fetch_all

    sources = pd.DataFrame(fetch_all(
        "SELECT source_id, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM sources"))
    labels = lb.assign(pd.Index(sources["source_id"]), lb.groups_near())
    lb.write(labels.assign(landcover=pd.NA))
    sx, sy = to_metres(sources["lat"], sources["lon"])
    for spec in SOURCES:
        if spec.expected != "registered":
            continue
        x, y = to_metres([spec.lat], [spec.lon])
        nearest = sources["source_id"].iat[int(np.argmin(np.hypot(sx - x[0], sy - y[0])))]
        assert labels.loc[nearest, "label"] == spec.cls, spec.name
    stored = fetch_all("SELECT count(*) AS n, count(label) AS labelled FROM source_labels")[0]
    assert stored["n"] == len(sources) and stored["labelled"] == len(sources)
    named = fetch_all("SELECT count(*) AS n FROM sources WHERE label_source LIKE 'osm:%'")[0]
    assert named["n"] == len(sources)
