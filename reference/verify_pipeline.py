"""Synthetic end-to-end verification of the PS 26162 pipeline."""
import numpy as np
import pandas as pd
from scipy.stats import entropy
from sklearn.cluster import DBSCAN
from sklearn.metrics import classification_report
from sklearn.model_selection import GroupKFold
from xgboost import XGBClassifier

rng = np.random.default_rng(42)
YEARS, DAYS = 6, 2191

# class -> (temp_K, temp_sd, area_m2, persistence, frp_med, frp_cv, night_ratio, months)
SPEC = {
 "flare":    (1750, 60,   15,  0.93,  45, 0.18, 0.95, list(range(1,13))),
 "furnace":  (1300, 110,  400, 0.60, 180, 0.35, 0.60, list(range(1,13))),
 "kiln":     (1150, 90,   900, 0.35,  60, 0.40, 0.55, [11,12,1,2,3,4,5]),
 "mining":   ( 900, 70,  2500, 0.50,  40, 0.45, 0.50, list(range(1,13))),
 "cropland": ( 920, 80,  9000, 0.045, 90, 0.55, 0.30, [10,11,4,5]),
 "forest":   ( 950, 95,  7000, 0.025,200, 0.70, 0.35, [3,4,5,6]),
}
N_PER = {"flare":55,"furnace":50,"kiln":60,"mining":40,"cropland":65,"forest":55}
STATES = ["GJ","MH","PB","HR","OD","JH","CG","TN","UP","RJ"]

dates = pd.date_range("2020-01-01", periods=DAYS, freq="D")
month = dates.month.values
# cloud: heavy Jun-Sep monsoon, light otherwise -> P(clear look)
clear_p = np.where(np.isin(month,[6,7,8,9]), 0.28, 0.82)
observed = rng.random(DAYS) < clear_p          # shared observability mask
print(f"Observability: {observed.mean():.1%} of days had a clear look "
      f"(monsoon {observed[np.isin(month,[6,7,8,9])].mean():.1%})")

HETERO = 0.45   # per-source spread within a class
LABEL_NOISE = 0.12  # OSM/FSI weak supervision is imperfect
VNF_MISS = 0.55     # night-only + Planck-fit failures
rows, truth = [], []
sid = 0
for cls,(T,Tsd,A,pers,frpm,cv,nr,mons) in SPEC.items():
    for _ in range(N_PER[cls]):
        sid += 1
        T_i    = T   * np.exp(rng.normal(0, 0.10*HETERO*2))
        A_i    = A   * np.exp(rng.normal(0, 1.10*HETERO))
        pers_i = np.clip(pers * np.exp(rng.normal(0, 0.85*HETERO)), 0.004, 0.98)
        frpm_i = frpm* np.exp(rng.normal(0, 1.00*HETERO))
        cv_i   = cv  * np.exp(rng.normal(0, 0.30*HETERO))
        nr_i   = np.clip(nr + rng.normal(0, 0.18), 0.05, 0.98)
        mons_i = mons if rng.random() > 0.15 else sorted(set(mons) | {int(rng.integers(1,13))})
        lat = rng.uniform(8.5, 32.0)
        lon = rng.uniform(70.0, 88.0)
        if rng.random() < 0.18 and truth:            # 18% sit beside an existing source
            _,_,_, la, lo = truth[rng.integers(len(truth))]
            lat, lon = la + rng.normal(0,0.004), lo + rng.normal(0,0.004)
        st  = STATES[rng.integers(len(STATES))]
        in_season = np.isin(month, mons_i)
        # burn probability on an observed day
        p = np.where(in_season, pers_i*(12/len(mons_i))*0.85, pers_i*0.02)
        fires = observed & (rng.random(DAYS) < np.clip(p,0,0.98))
        n = fires.sum()
        if n < 8:            # too sparse to ever cluster
            continue
        idx = np.where(fires)[0]
        frp = frpm_i*np.exp(rng.normal(0, cv_i, n))            # right-skewed
        tmp = rng.normal(T_i, Tsd, n)
        tmp[rng.random(n) < VNF_MISS] = np.nan
        night = rng.random(n) < nr_i
        # detections jitter ~200 m around the source
        rows.append(pd.DataFrame({
            "sid": sid, "cls": cls, "state": st,
            "lat": lat + rng.normal(0,0.0018,n), "lon": lon + rng.normal(0,0.0018,n),
            "day": idx, "month": month[idx], "frp": frp,
            "temp": tmp, "area": A_i*np.exp(rng.normal(0,0.3,n)),
            "night": night}))
        truth.append((sid, cls, st, lat, lon))

det = pd.concat(rows, ignore_index=True)
truth = pd.DataFrame(truth, columns=["sid","cls","state","lat","lon"])
print(f"Simulated {len(truth)} sources, {len(det):,} detections over {YEARS} years\n")

# ---------- STEP 1: DBSCAN clustering in metres ----------
x = det.lon.values*111320*np.cos(np.radians(det.lat.values))
y = det.lat.values*110540
lab = DBSCAN(eps=500, min_samples=5, algorithm="ball_tree", n_jobs=2).fit_predict(np.c_[x,y])
det["cluster"] = lab
noise = (lab==-1).mean()
found = det[lab>=0].groupby("cluster").sid.agg(lambda s: s.mode()[0]).nunique()
purity = det[lab>=0].groupby("cluster").sid.agg(lambda s: (s==s.mode()[0]).mean()).mean()
print(f"DBSCAN: {det.cluster.nunique()-1} clusters for {len(truth)} true sources")
print(f"  recovered {found}/{len(truth)} sources | mean purity {purity:.3f} | noise {noise:.2%}\n")

# ---------- STEP 2: fingerprints ----------
obs_nights = observed.sum()
def fingerprint(g):
    f = g.frp.values
    mc = g.month.value_counts(normalize=True)
    return pd.Series({
        "temp_p50": np.nanmedian(g.temp), "temp_p90": np.nanquantile(g.temp,.9),
        "temp_iqr": np.subtract(*np.nanquantile(g.temp,[.75,.25])),
        "temp_cov": g.temp.notna().mean(),
        "area_p50": np.median(g.area),
        "frp_p50": np.median(f), "frp_p95": np.quantile(f,.95),
        "frp_mad": np.median(np.abs(f-np.median(f))),
        "persistence": g.day.nunique()/obs_nights,
        "night_ratio": g.night.mean(),
        "month_entropy": entropy(mc.reindex(range(1,13),fill_value=1e-9)),
        "n_det": len(g)})

cl = det[det.cluster>=0]
fp = cl.groupby("cluster").apply(fingerprint, include_groups=False).reset_index()
meta = cl.groupby("cluster").agg(cls=("cls",lambda s:s.mode()[0]), state=("state",lambda s:s.mode()[0]))
fp = fp.merge(meta, on="cluster")
FEATS = ["temp_p50","temp_p90","temp_iqr","area_p50","frp_p50","frp_p95",
         "frp_mad","persistence","night_ratio","month_entropy","n_det","temp_cov"]

# ---------- STEP 3: classifier, thermal features only, split by state ----------
codes, names = pd.factorize(fp.cls)
oof = np.empty(len(fp), dtype=int)
for tr,te in GroupKFold(5).split(fp[FEATS], codes, groups=fp.state):
    m = XGBClassifier(n_estimators=400, max_depth=6, learning_rate=0.05,
                      subsample=.8, colsample_bytree=.8, verbosity=0)
    y_tr = codes[tr].copy()
    flip = rng.random(len(y_tr)) < LABEL_NOISE
    y_tr[flip] = rng.integers(0, len(names), flip.sum())
    m.fit(fp[FEATS].iloc[tr], y_tr)
    oof[te] = m.predict(fp[FEATS].iloc[te])
acc = (oof==codes).mean()
print("SOURCE CLASSIFIER  (thermal features only, no location, GroupKFold by state)")
print(f"  overall accuracy {acc:.1%}   [benchmark to beat: 77%]")
print(f"  degradations applied: {HETERO:.0%} within-class spread, {LABEL_NOISE:.0%} label noise, {VNF_MISS:.0%} temp missing\n")
print(classification_report(codes, oof, target_names=names, digits=3, zero_division=0))

m = XGBClassifier(n_estimators=400, max_depth=6, learning_rate=0.05, verbosity=0)
m.fit(fp[FEATS], codes)
imp = pd.Series(m.feature_importances_, index=FEATS).sort_values(ascending=False)
print("Top features:", ", ".join(f"{k} {v:.2f}" for k,v in imp.head(4).items()), "\n")

# ---------- STEP 4: anomaly test (Road C) ----------
def baseline(f):
    med = np.median(f)
    mad = np.median(np.abs(f-med))
    return med, max(mad,1e-6), np.quantile(f,.99)

def flag(v, b):
    med,mad,p99 = b
    return (0.6745*(v-med)/mad > 3.5) and (v > p99*1.5)

fp_rate = []
tp_rate = []
for _, g in cl.groupby("cluster"):
    if len(g) < 60:
        continue
    f = g.frp.values
    hist, live = f[:-30], f[-30:]
    b = baseline(hist)
    fp_rate.append(np.mean([flag(v,b) for v in live]))          # normal ops
    inj = np.median(hist)*rng.uniform(6,12,30)                   # injected fires
    tp_rate.append(np.mean([flag(v,b) for v in inj]))
print("ANOMALY TEST  (z>3.5 AND >1.5x p99, per-source baseline)")
print(f"  false positives on normal operation : {np.mean(fp_rate):.3%} of passes")
print(f"  detection of injected 6-12x events  : {np.mean(tp_rate):.1%}")
print(f"  sources evaluated                   : {len(fp_rate)}")
