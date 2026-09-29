"""Model 1: what kind of industry is this source, from its thermal fingerprint alone.

The only learned model in the system. It sees how a source burns -- how often,
how hot, how steadily, in which seasons -- and never where it is: every label is
built from location (``firewatch.models.labels``), so a location feature would
teach it the labelling rule. ``check_leakage`` enforces that, and ``scripts/
train.py`` calls it before anything else.

Absolute brightness temperatures are left out too. A pixel's I4 and I5
temperatures carry the background's climate as much as the fire -- Rajasthan in
May against Assam in the monsoon -- which makes them a location proxy. The I4 - I5
contrast carries the fire and stays.

Evaluation is spatial. Sources cluster (Jharia alone is 14), and neighbours share
both labels and fingerprints, so a random split would test the model on the twin
of a training source. GroupKFold on 2-degree blocks keeps each block's sources on
one side. Hyperparameters are fixed and deliberately plain; nothing is tuned on
the cross-validation score it reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

#: Model 1's inputs: fingerprint features, none of them location.
FEATS = [
    "n_cells", "width_km", "years_active", "det_per_year",
    "persistence_night", "persistence_day", "night_share",
    "frp_med", "frp_mad", "frp_p90", "frp_p99", "frp_robust_cv",
    "frp_night_med", "frp_day_med",
    "month_entropy", "peak_month_share", "monsoon_share",
    "bt45_night_med", "bt45_day_med",
    "temp_med", "temp_p90", "temp_cov", "area_med",
]
#: Fingerprint features Model 1 does not use, and why.
EXCLUDED = {
    "n_det": "redundant with det_per_year, and grows with how long the archive is",
    "bt4_night_med": "absolute brightness carries background climate: a location proxy",
    "bt4_day_med": "absolute brightness carries background climate: a location proxy",
    "bt5_night_med": "absolute brightness carries background climate: a location proxy",
}
PARAMS = {"n_estimators": 300, "max_depth": 3, "learning_rate": 0.05, "subsample": 0.8,
          "colsample_bytree": 0.8, "min_child_weight": 2, "reg_lambda": 1.0,
          "tree_method": "hist", "eval_metric": "mlogloss"}
BLOCK_DEG = 2.0
N_SPLITS = 5
SEED = 26162


class LeakageError(AssertionError):
    """A label input reached the feature list."""


def check_leakage(feats: list[str], barred: set[str]) -> None:
    """Fail loudly if any feature is something a label was built from."""
    leaked = sorted(set(feats) & barred)
    if leaked:
        raise LeakageError(
            f"FEATS contains label inputs or location: {leaked}. Every weak label is "
            "built from location; a model that sees it learns the labelling rule.")


def spatial_blocks(lat, lon, deg: float = BLOCK_DEG) -> np.ndarray:
    """A group id per ``deg`` x ``deg`` block, for GroupKFold."""
    row = np.floor(np.asarray(lat, dtype=float) / deg).astype(np.int64)
    col = np.floor(np.asarray(lon, dtype=float) / deg).astype(np.int64)
    return row * 1000 + col


def usable(X: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    """Features that carry information, and why the rest were dropped."""
    keep, dropped = [], {}
    for c in X.columns:
        col = X[c].astype(float)
        if col.isna().all():
            dropped[c] = "missing for every source (VNF not loaded)"
        elif col.nunique(dropna=True) <= 1 and not col.isna().any():
            dropped[c] = "constant"
        elif col.nunique(dropna=True) <= 1:
            dropped[c] = "constant where present"
        else:
            keep.append(c)
    return keep, dropped


def balanced_weights(y: np.ndarray) -> np.ndarray:
    counts = np.bincount(y)
    return (len(y) / (len(counts) * counts))[y]


def make_model(n_classes: int, seed: int = SEED):
    from xgboost import XGBClassifier
    return XGBClassifier(objective="multi:softprob", num_class=n_classes,
                         random_state=seed, n_jobs=4, **PARAMS)


def cross_validate(X: pd.DataFrame, y: np.ndarray, groups: np.ndarray, n_classes: int,
                   n_splits: int = N_SPLITS) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold class probabilities, and each row's fold."""
    from sklearn.model_selection import GroupKFold

    proba = np.zeros((len(y), n_classes))
    fold = np.full(len(y), -1)
    for k, (tr, te) in enumerate(GroupKFold(n_splits=n_splits).split(X, y, groups)):
        model = make_model(n_classes)
        model.fit(X.iloc[tr], y[tr], sample_weight=balanced_weights(y[tr]))
        proba[te] = model.predict_proba(X.iloc[te])
        fold[te] = k
    return proba, fold


def scores(y: np.ndarray, pred: np.ndarray, classes: list[str]) -> dict:
    from sklearn.metrics import (
        accuracy_score,
        balanced_accuracy_score,
        confusion_matrix,
        f1_score,
        precision_recall_fscore_support,
    )

    p, r, f, s = precision_recall_fscore_support(y, pred, labels=range(len(classes)),
                                                 zero_division=0)
    return {
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "balanced_accuracy": round(float(balanced_accuracy_score(y, pred)), 4),
        "macro_f1": round(float(f1_score(y, pred, average="macro")), 4),
        "per_class": {c: {"precision": round(float(p[i]), 4), "recall": round(float(r[i]), 4),
                          "f1": round(float(f[i]), 4), "support": int(s[i])}
                      for i, c in enumerate(classes)},
        "confusion": {"rows_true_cols_pred": classes,
                      "matrix": confusion_matrix(y, pred, labels=range(len(classes))).tolist()},
    }


def shap_importance(model, X: pd.DataFrame, classes: list[str]) -> dict[str, dict[str, float]]:
    """Mean |SHAP| per feature, overall and per class (XGBoost's exact TreeSHAP)."""
    from xgboost import DMatrix

    contrib = model.get_booster().predict(DMatrix(X, missing=np.nan), pred_contribs=True)
    contrib = np.abs(contrib[:, :, :-1])                  # drop the bias column
    out = {"overall": dict(zip(X.columns, contrib.mean(axis=(0, 1)).round(4).tolist(),
                               strict=True))}
    for i, c in enumerate(classes):
        out[c] = dict(zip(X.columns, contrib[:, i, :].mean(axis=0).round(4).tolist(),
                          strict=True))
    return {k: dict(sorted(v.items(), key=lambda kv: -kv[1])) for k, v in out.items()}


@dataclass
class Model1:
    """A fitted Model 1 and what it needs to predict."""

    model: object
    classes: list[str]
    features: list[str]

    def predict(self, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Class name and probability per row."""
        proba = self.model.predict_proba(X[self.features])
        return np.asarray(self.classes)[proba.argmax(axis=1)], proba.max(axis=1)

    def save(self, directory: Path, meta: dict) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "model1.json"
        self.model.save_model(path)
        (directory / "model1.meta.json").write_text(json.dumps(
            {"classes": self.classes, "features": self.features, **meta}, indent=2,
            default=str), encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path) -> Model1:
        from xgboost import XGBClassifier
        meta = json.loads((directory / "model1.meta.json").read_text(encoding="utf-8"))
        model = XGBClassifier()
        model.load_model(directory / "model1.json")
        return cls(model=model, classes=meta["classes"], features=meta["features"])


def fit(X: pd.DataFrame, y: np.ndarray, classes: list[str]) -> Model1:
    model = make_model(len(classes))
    model.fit(X, y, sample_weight=balanced_weights(y))
    return Model1(model=model, classes=classes, features=list(X.columns))
