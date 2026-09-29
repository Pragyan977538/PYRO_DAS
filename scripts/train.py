"""Stage 4: train Model 1 on the weak labels and report how good it honestly is.

    python scripts/train.py

1. The leakage guard: FEATS may share nothing with LABEL_INPUTS (or location, or
   the evaluation-only references). It runs first and fails loudly.
2. Block cross-validation (GroupKFold on 2-degree blocks) for every reported score,
   with two references that are never features: the majority class, and a
   location-only model showing what geography alone would score.
3. The final model, fitted on every labelled source, predicts every source; the
   prediction goes to ``sources.cls`` / ``cls_conf``.

Writes reports/model1_metrics.json and saves the model under DATA_DIR/models.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.db import fetch_all, get_conn  # noqa: E402
from firewatch.models import labels as lb  # noqa: E402
from firewatch.models import source_clf as clf  # noqa: E402
from firewatch.registry.fingerprint import FEATURES  # noqa: E402

METRICS = REPO / "reports" / "model1_metrics.json"
OIL_GAS_USABLE = 0.5      # recall and precision, block CV: fixed before training
N_PERMUTATIONS = 100      # label shuffles for the permutation test
log = logging.getLogger("train")


def guard() -> None:
    """Fail loudly before any data is touched if a label input is a feature."""
    clf.check_leakage(clf.FEATS, lb.barred_inputs())
    unknown = sorted(set(clf.FEATS) - set(FEATURES))
    if unknown:
        raise SystemExit(f"FEATS names features the fingerprint does not have: {unknown}")


def read() -> pd.DataFrame:
    rows = fetch_all("""
        SELECT s.source_id, ST_Y(s.geom) AS lat, ST_X(s.geom) AS lon, s.fingerprint,
               l.label, l.label_group, l.groups_near
          FROM sources s LEFT JOIN source_labels l USING (source_id)
         WHERE NOT s.provisional ORDER BY s.source_id""")
    frame = pd.DataFrame(rows).set_index("source_id")
    fp = pd.DataFrame(list(frame.pop("fingerprint")), index=frame.index)
    return frame.join(fp)


def contested(groups_near) -> bool:
    classes = {lb.CLASS_OF.get(g) for g in (groups_near or {})} - {None}
    return len(classes) > 1


def main() -> int:
    guard()
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")
    data = read()
    if data.empty or data["label"].isna().all():
        print("no labelled sources: run `make registry` and `make labels` first")
        return 1

    X_all = data[clf.FEATS].astype(float)
    feats, dropped = clf.usable(X_all)
    X_all = X_all[feats]
    present = [c for c in [*lb.CLASSES, lb.BIOMASS] if (data["label"] == c).any()]
    train = data["label"].isin(present).to_numpy()
    y = pd.Categorical(data.loc[train, "label"], categories=present).codes.astype(int)
    X = X_all[train]
    groups = clf.spatial_blocks(data.loc[train, "lat"], data.loc[train, "lon"])
    log.info("%d labelled sources in %d blocks; %d features (%d dropped)", len(y),
             len(set(groups)), len(feats), len(dropped))

    proba, fold = clf.cross_validate(X, y, groups, len(present))
    pred = proba.argmax(axis=1)
    cv = clf.scores(y, pred, present)

    # References -- never features.
    loc = data.loc[train, ["lat", "lon"]].astype(float)
    loc_proba, _ = clf.cross_validate(loc, y, groups, len(present))
    majority = int(np.bincount(y).argmax())
    # Permutation test: the same block CV on shuffled labels, many times. Balanced
    # accuracy under the null is ~1/k on average but noisy with 34 oil and gas
    # sources, so one shuffle proves little; the distribution gives a p-value.
    rng = np.random.default_rng(clf.SEED)
    null = []
    for _ in range(N_PERMUTATIONS):
        shuffled = rng.permutation(y)
        shuf_proba, _ = clf.cross_validate(X, shuffled, groups, len(present))
        null.append(clf.scores(shuffled, shuf_proba.argmax(axis=1), present)
                    ["balanced_accuracy"])
    null = np.array(null)
    disputed = np.array([contested(g) for g in data.loc[train, "groups_near"]])

    og = cv["per_class"].get("oil_gas", {})
    oil_gas_usable = bool(og and og["recall"] >= OIL_GAS_USABLE
                          and og["precision"] >= OIL_GAS_USABLE)

    final = clf.fit(X, y, present)
    cls, conf = final.predict(X_all)
    importance = clf.shap_importance(final.model, X, present)
    gain = final.model.get_booster().get_score(importance_type="gain")
    unlabelled = ~train
    model_dir = settings().data_dir / "models"
    final.save(model_dir, {"params": clf.PARAMS, "trained_on": int(len(y)),
                           "block_deg": clf.BLOCK_DEG})

    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany("UPDATE sources SET cls = %s, cls_conf = %s WHERE source_id = %s",
                        [(str(c), round(float(p), 4), int(sid))
                         for sid, c, p in zip(data.index, cls, conf, strict=True)])

    metrics = {
        "model": "XGBoost, multi:softprob", "params": clf.PARAMS,
        "features_used": feats, "features_dropped": dropped,
        "features_excluded": clf.EXCLUDED,
        "leakage_guard": {"feats": clf.FEATS, "label_inputs":
                          {k: sorted(v) for k, v in lb.LABEL_INPUTS.items()},
                          "intersection": []},
        "labels": {
            "per_class": {c: int((data["label"] == c).sum()) for c in present},
            "per_class_and_source": {
                f"{c} <- {g}": int(n) for (c, g), n in
                data[train].groupby(["label", "label_group"]).size().items()},
            "unlabelled": int(data["label"].isna().sum()),
        },
        "evaluation": {"scheme": f"GroupKFold({clf.N_SPLITS}) on {clf.BLOCK_DEG:g}-degree "
                                 "blocks", "blocks": int(len(set(groups))),
                       "trained_on": int(len(y))},
        "block_cv": cv,
        "block_cv_uncontested": clf.scores(y[~disputed], pred[~disputed], present)
        if (~disputed).any() else None,
        "block_cv_contested": {"sources": int(disputed.sum()),
                               "accuracy": round(float((pred[disputed] == y[disputed])
                                                       .mean()), 4) if disputed.any() else None},
        "references": {
            "majority_class": {"class": present[majority],
                               "accuracy": round(float((y == majority).mean()), 4),
                               "balanced_accuracy": round(1 / len(present), 4)},
            "location_only_lat_lon": {
                k: v for k, v in clf.scores(y, loc_proba.argmax(axis=1), present).items()
                if k in ("accuracy", "balanced_accuracy", "macro_f1")},
            "permutation_test": {
                "permutations": N_PERMUTATIONS, "metric": "balanced_accuracy",
                "null_mean": round(float(null.mean()), 4),
                "null_max": round(float(null.max()), 4),
                "observed": cv["balanced_accuracy"],
                "p_value": round(float((1 + (null >= cv["balanced_accuracy"]).sum())
                                       / (1 + N_PERMUTATIONS)), 4)},
            "published_benchmark": "77% (Liu et al. 2018, VNF industrial sub-types, global)",
        },
        "oil_gas": {"recall": og.get("recall"), "precision": og.get("precision"),
                    "usable_threshold": OIL_GAS_USABLE, "usable": oil_gas_usable,
                    "fallback": None if oil_gas_usable else
                    "map names the nearest OSM oil and gas facility (display lookup)"},
        "importance_mean_abs_shap": importance,
        "importance_gain": dict(sorted(((k, round(v, 3)) for k, v in gain.items()),
                                       key=lambda kv: -kv[1])),
        "predictions": {
            "all_sources": {str(k): int(v) for k, v in pd.Series(cls).value_counts().items()},
            "unlabelled_sources": {str(k): int(v) for k, v in
                                   pd.Series(cls[unlabelled]).value_counts().items()},
            "median_confidence": round(float(np.median(conf)), 4),
        },
        "model_file": str(model_dir / "model1.json"),
    }
    METRICS.write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    summary = {k: metrics[k] for k in ("labels", "evaluation", "references", "oil_gas")}
    summary["block_cv"] = {k: cv[k] for k in ("accuracy", "balanced_accuracy", "macro_f1",
                                              "per_class", "confusion")}
    print(json.dumps(summary, indent=2))
    print(f"wrote {METRICS.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
