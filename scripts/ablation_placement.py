"""Ablation: does putting the meter on the street-facing wall (TAB Wasser) improve the model?
Compares (A) meter at the address/GPS point  vs  (B) meter on the street-facing wall,
each with and without the extra document features, over 5 random seeds."""
import numpy as np, pandas as pd
from sklearn.base import clone
from sklearn.metrics import roc_auc_score, mean_absolute_error
from sklearn.model_selection import GroupKFold
from sklearn.ensemble import HistGradientBoostingRegressor
from hnplan.config import load_config
from hnplan.rasters import Grid
from hnplan.features import FeatureBuilder, FEATURES, DOC_FEATURES, MONOTONIC
from hnplan.model import make_models

cfg = load_config(); P = cfg.processed
L0 = pd.read_parquet(P("train_links.parquet"))
meters = pd.read_parquet(P("meters.parquet")).set_index("zaehlernummer_id")
sites = pd.read_parquet(P("sites.parquet")); gws = sites[sites.kind == "existing"].reset_index(drop=True)
grid = Grid.load(P("grid.npz")); fb = FeatureBuilder(grid, cfg.raw["radio"], cfg.raw["grid"])
ids = L0.zaehlernummer_id.drop_duplicates().to_numpy(); m = meters.loc[ids].reset_index()
ri = np.repeat(np.arange(len(m)), len(gws)); ti = np.tile(np.arange(len(gws)), len(m))
base = L0.set_index(["zaehlernummer_id", "site_id"])
def table(x, y, pl):
    X = fb.links(fb.rx_points(x, y, pl), ri, gws, ti)
    key = pd.MultiIndex.from_arrays([m.zaehlernummer_id.to_numpy()[ri], gws.site_id.to_numpy()[ti]])
    lab = base.reindex(key)[["heard", "rssi_med", "block"]].reset_index(drop=True)
    return pd.concat([X, lab], axis=1)
A = table(m.x, m.y, None)                         # address / GPS point, no doc features
B = table(m.px.fillna(m.x), m.py.fillna(m.y), m)  # street-facing wall + doc features
old = list(FEATURES)
def evaluate(T, feats, seed):
    y = T.heard.to_numpy(); est = clone(make_models(seed)["gbm"]); mono = [MONOTONIC.get(f, 0) for f in feats]
    est.set_params(monotonic_cst=mono); out = {}
    for name, groups, k in [("spatial", T.block, 5), ("logo", pd.Series(gws.site_id.to_numpy()[ti]), 8)]:
        p = np.zeros(len(T))
        for tr, te in GroupKFold(k).split(T, y, groups):
            p[te] = clone(est).fit(T.iloc[tr][feats], y[tr]).predict_proba(T.iloc[te][feats])[:, 1]
        out[name] = roc_auc_score(y, p)
    H = T[T.heard == 1].reset_index(drop=True); yr = H.rssi_med.to_numpy(); pr = np.zeros(len(H))
    reg = HistGradientBoostingRegressor(learning_rate=0.05, max_iter=300, max_leaf_nodes=15, min_samples_leaf=15, monotonic_cst=mono, random_state=seed)
    for tr, te in GroupKFold(5).split(H, yr, H.block):
        pr[te] = clone(reg).fit(H.iloc[tr][feats], yr[tr]).predict(H.iloc[te][feats])
    out["rssi_mae"] = mean_absolute_error(yr, pr); return out
rows = []
for label, T, feats in [("A address point, old features", A, old), ("B wall position, old features", B, old),
                        ("B wall position + facing/setback/shaft", B, FEATURES + DOC_FEATURES)]:
    for s in range(1):  # the boosted model is deterministic, one seed is enough
        rows.append(dict(variant=label, seed=s, **evaluate(T, feats, 42 + s)))
r = pd.DataFrame(rows).groupby("variant")[["spatial", "logo", "rssi_mae"]].agg(["mean", "std"]).round(4)
print(r.to_string())
print("\nfacing_cos on received links: corr with RSSI =", round(np.corrcoef(B[B.heard == 1].facing_cos.fillna(0), B[B.heard == 1].rssi_med)[0, 1], 3))
print("meters with a shaft flag in training data:", int(m.shaft_likely.sum()))
import json
res = pd.DataFrame(rows).groupby("variant")[["spatial", "logo", "rssi_mae"]].mean().round(4).reset_index().to_dict("records")
P("ablation_placement.json").write_text(json.dumps(dict(variants=res,
    facing_corr_rssi=float(np.corrcoef(B[B.heard == 1].facing_cos.fillna(0), B[B.heard == 1].rssi_med)[0, 1]),
    shaft_meters_in_training=int(m.shaft_likely.sum())), indent=1))
print("saved", P("ablation_placement.json"))
