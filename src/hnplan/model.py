"""Reception model: P(gateway hears an end device at this location) + expected RSSI.

Candidates
  physics  – logistic regression on distance + antenna height (textbook log-distance idea)
  catboost – ordered gradient boosting with the same monotonic constraints (benchmark winner)
  gbm      – gradient boosting with physically sensible monotonic constraints
             (more distance / more obstruction can never *increase* the probability)
  mlp      – neural network (scaled features, 2 hidden layers)

Validation
  spatial  – 5-fold CV where whole 1 km blocks are held out (no neighbour leakage)
  LOGO     – leave-one-gateway-out: the model never saw the test gateway. This is the
             honest test for the planning question "how would a NEW site perform?"
"""
from __future__ import annotations

import json

import joblib
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .config import Config
from .features import FEATURES, MONOTONIC

# Meter manufacturer = 3 letters after the first digit of the meter number (8APA… -> APA). Within the same
# 1 km blocks Apator meters are heard far less often than IMT meters at equal distance (logit −1.08 after
# distance + antenna height) – a device effect (internal antenna, installation). IMT is the reference.
DEVICE_FEATURES = ["is_apa", "is_axi"]
MAKERS = {"IMT": {}, "APA": {"is_apa": 1.0}, "AXI": {"is_axi": 1.0}}
MODEL_FEATURES = FEATURES + DEVICE_FEATURES


def maker_of(meter_ids: pd.Series) -> pd.Series:
    return meter_ids.astype(str).str[1:4]


def add_device_flags(L: pd.DataFrame) -> pd.DataFrame:
    mk = maker_of(L["zaehlernummer_id"])
    return L.assign(is_apa=(mk == "APA").astype(float), is_axi=(mk == "AXI").astype(float))


class CatBoostMono(ClassifierMixin, BaseEstimator):
    """Monotone-constrained CatBoost. Thin wrapper because CatBoost rewrites its
    `monotone_constraints` parameter internally, which breaks sklearn.clone."""

    def __init__(self, iterations=600, learning_rate=0.03, depth=5, l2_leaf_reg=3.0, seed=0):
        self.iterations, self.learning_rate, self.depth, self.l2_leaf_reg, self.seed = \
            iterations, learning_rate, depth, l2_leaf_reg, seed

    def fit(self, X, y):
        import catboost as cb
        self.m_ = cb.CatBoostClassifier(iterations=self.iterations, learning_rate=self.learning_rate, depth=self.depth,
                                        l2_leaf_reg=self.l2_leaf_reg,
                                        monotone_constraints=[MONOTONIC.get(f, 0) for f in getattr(X, "columns", MODEL_FEATURES)],
                                        random_seed=self.seed, verbose=0, thread_count=4, allow_writing_files=False)
        self.m_.fit(X, y)
        self.classes_ = self.m_.classes_
        return self

    def predict_proba(self, X):
        return self.m_.predict_proba(X)


def make_models(seed: int, feats=None) -> dict:
    mono = [MONOTONIC.get(f, 0) for f in (feats or MODEL_FEATURES)]
    return {
        # best in `hnplan benchmark` (13 learners): lowest leave-one-gateway-out log-loss
        "catboost": CatBoostMono(seed=seed),
        "physics": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                 LogisticRegression(max_iter=1000)),
        "gbm": HistGradientBoostingClassifier(learning_rate=0.05, max_iter=300, max_leaf_nodes=15,
                                              min_samples_leaf=20, l2_regularization=1.0,
                                              monotonic_cst=mono, random_state=seed),
        "mlp": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                             MLPClassifier(hidden_layer_sizes=(64, 32), alpha=1e-3, max_iter=800,
                                           early_stopping=True, n_iter_no_change=25, random_state=seed)),
    }


PHYSICS_COLS = ["log_d", "tx_h_agl"]
# set to NaN by FeatureBuilder when a path has no usable surface-model geometry
GEOMETRY = ["obs_max_m", "terr_obs_max_m", "obs_frac", "fresnel_frac", "terr_obs_frac", "dz_m"]


def _cols(name, feats=None):
    return PHYSICS_COLS if name == "physics" else (feats or MODEL_FEATURES)


def _scores(y, p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return dict(auc=float(roc_auc_score(y, p)) if len(set(y)) > 1 else float("nan"),
                brier=float(brier_score_loss(y, p)), logloss=float(log_loss(y, p, labels=[0, 1])))


def cross_validate(L: pd.DataFrame, seed: int, feats=None) -> tuple[dict, dict]:
    y = L["heard"].to_numpy()
    models = make_models(seed, feats)
    res, oof = {}, {}
    schemes = {"spatial": (L["block"], GroupKFold(n_splits=5)),
               "logo": (L["site_id"], GroupKFold(n_splits=L["site_id"].nunique()))}
    for sname, (groups, cv) in schemes.items():
        for mname, est in models.items():
            p = np.zeros(len(L))
            for tr, te in cv.split(L, y, groups):
                m = clone(est).fit(L.iloc[tr][_cols(mname, feats)], y[tr])
                p[te] = m.predict_proba(L.iloc[te][_cols(mname, feats)])[:, 1]
            res[f"{sname}/{mname}"] = _scores(y, p)
            oof[f"{sname}/{mname}"] = p
    # device-level check (spatial CV): P(heard by >=1 known gateway) vs reality
    for mname in models:
        p = oof[f"spatial/{mname}"]
        dev = L.assign(p=p).groupby("zaehlernummer_id").agg(
            p_any=("p", lambda s: 1 - np.prod(1 - s)), y_any=("heard", "max"))
        res[f"device/{mname}"] = dict(_scores(dev.y_any.to_numpy(), dev.p_any.to_numpy()),
                                     mean_pred=float(dev.p_any.mean()), mean_true=float(dev.y_any.mean()))
    return res, oof


# Absolute ground elevation (m a.s.l.) is site-specific: Heilbronn's meters lie at 157–218 m, Stuttgart's at
# 225–385 m. For other cities a "transfer" model without it is trained on the same Heilbronn data (city.py).
TRANSFER_DROP = ("rx_ground_z",)


def train(cfg: Config, drop: tuple = (), tag: str = "") -> dict:
    """tag="" -> model.joblib / metrics.json (Heilbronn); tag="transfer" -> model_transfer.joblib (other cities)."""
    FEATS = [f for f in MODEL_FEATURES if f not in drop]
    mc = cfg.raw["model"]
    seed = mc["random_state"]
    L = add_device_flags(pd.read_parquet(cfg.processed("train_links.parquet")))
    meters = L.drop_duplicates("zaehlernummer_id")
    mix = maker_of(meters["zaehlernummer_id"]).value_counts(normalize=True)
    device_mix = {m: float(mix.get(m, 0.0)) for m in MAKERS if mix.get(m, 0.0) > 0}
    print(f"[train] {len(L):,} links, {L.heard.mean():.1%} positive, {L.site_id.nunique()} gateways, "
          f"{L.block.nunique()} spatial blocks; meter mix " + ", ".join(f"{k} {v:.0%}" for k, v in device_mix.items()))
    res, oof = cross_validate(L, seed, FEATS)
    for k, v in res.items():
        print(f"   {k:16s} AUC {v['auc']:.3f}  Brier {v['brier']:.3f}" +
              (f"  pred {v['mean_pred']:.2f} vs true {v['mean_true']:.2f}" if "mean_pred" in v else ""))

    kind = mc["kind"]
    if kind == "auto":
        # lowest leave-one-gateway-out log-loss: P is used as a probability by the optimiser,
        # so calibration matters as much as ranking (AUC)
        kind = min(["catboost", "gbm", "mlp"], key=lambda k: (res[f"logo/{k}"]["logloss"], -res[f"logo/{k}"]["auc"]))
    print(f"[train] selected model: {kind}")

    # ---- final bootstrap ensemble (resampling whole spatial blocks) -> mean + uncertainty
    rng = np.random.default_rng(seed)
    blocks = L["block"].unique()
    y = L["heard"].to_numpy()
    ens, fb = [], []
    for b in range(mc["ensemble_size"]):
        pick = rng.choice(blocks, size=len(blocks), replace=True)
        idx = np.concatenate([np.flatnonzero(L["block"].to_numpy() == k) for k in pick])
        if len(set(y[idx])) < 2:
            continue
        m = clone(make_models(seed + b, FEATS)[kind]).fit(L.iloc[idx][FEATS], y[idx])
        ens.append(m)
        # distance + antenna-height model for links without line-of-sight geometry (see predict)
        fb.append(clone(make_models(seed + b)["physics"]).fit(L.iloc[idx][PHYSICS_COLS], y[idx]))

    # permutation importance on the full fit (which physical factors matter?)
    full = clone(make_models(seed, FEATS)[kind]).fit(L[FEATS], y)
    pi = permutation_importance(full, L[FEATS], y, scoring="roc_auc", n_repeats=5, random_state=seed)
    imp = sorted(zip(FEATS, pi.importances_mean), key=lambda t: -t[1])

    # ---- RSSI model on received links (log-distance baseline vs boosted)
    H = L[L.heard == 1].reset_index(drop=True)
    yr = H["rssi_med"].to_numpy()
    mono = [MONOTONIC.get(f, 0) for f in FEATS]  # stronger signal has the same direction as P
    reg = HistGradientBoostingRegressor(learning_rate=0.05, max_iter=300, max_leaf_nodes=15,
                                        min_samples_leaf=15, monotonic_cst=mono, random_state=seed)
    cvr = GroupKFold(n_splits=5)
    pr, pb = np.zeros(len(H)), np.zeros(len(H))
    for tr, te in cvr.split(H, yr, H["block"]):
        pr[te] = clone(reg).fit(H.iloc[tr][FEATS], yr[tr]).predict(H.iloc[te][FEATS])
        pb[te] = LinearRegression().fit(H.iloc[tr][["log_d"]], yr[tr]).predict(H.iloc[te][["log_d"]])
    rssi_res = dict(mae_model=float(mean_absolute_error(yr, pr)), mae_logdistance=float(mean_absolute_error(yr, pb)))
    print(f"[train] RSSI MAE: model {rssi_res['mae_model']:.2f} dB vs log-distance {rssi_res['mae_logdistance']:.2f} dB")
    reg_full = clone(reg).fit(H[FEATS], yr)
    ld = LinearRegression().fit(H[["log_d"]], yr)

    # measured reception per manufacturer (for the methodology panel)
    Lm = L.assign(maker=maker_of(L["zaehlernummer_id"]), db=pd.cut(L["d_m"], [0, 500, 1000, 2000, 8000]))
    by_maker = {m: dict(meters=int(g.zaehlernummer_id.nunique()),
                        heard_by_distance={str(k): float(v) for k, v in g.groupby("db", observed=True).heard.mean().items()})
                for m, g in Lm.groupby("maker")}

    bundle = dict(kind=kind, ensemble=ens, fallback=fb, rssi=reg_full, rssi_fallback=ld, features=FEATS,
                  device_mix=device_mix,
                  train_ranges={f: [float(np.nanpercentile(L[f], 1)), float(np.nanpercentile(L[f], 99))]
                                for f in FEATS})
    joblib.dump(bundle, cfg.processed(f"model{'_' + tag if tag else ''}.joblib"))
    metrics = dict(selected=kind, cv=res, rssi=rssi_res, importance=[(f, float(v)) for f, v in imp],
                   device_mix=device_mix, by_maker=by_maker,
                   log_distance=dict(intercept=float(ld.intercept_), slope_per_decade=float(ld.coef_[0])),
                   n_links=int(len(L)), n_pos=int(L.heard.sum()), n_devices=int(L.zaehlernummer_id.nunique()))
    cfg.processed(f"metrics{'_' + tag if tag else ''}.json").write_text(json.dumps(metrics, indent=1))
    print("[train] top factors:", ", ".join(f"{f} ({v:.3f})" for f, v in imp[:6]))
    return metrics


def predict(cfg: Config) -> pd.DataFrame:
    b = joblib.load(cfg.processed("model.joblib"))
    out = _predict(cfg, b, pd.read_parquet(cfg.processed("pred_links.parquet")),
                   pd.read_parquet(cfg.processed("cells.parquet")), write_devices=True)
    out.to_parquet(cfg.processed("predictions.parquet"))
    nogeo = out.no_geometry.to_numpy()
    print(f"[predict] {len(out):,} links ({nogeo.mean():.1%} without geometry -> distance model), "
          f"mean P {out.p.mean():.3f} (today's meter mix) / {out.p_weak.mean():.3f} (weakest type), "
          f"links with P>0.5: {(out.p > 0.5).sum():,}")
    if cfg.processed("pred_links_future.parquet").exists() and cfg.processed("future_cells.parquet").exists():
        from .pipeline import future_cell_table
        fut = _predict(cfg, b, pd.read_parquet(cfg.processed("pred_links_future.parquet")), future_cell_table(cfg))
        fut.to_parquet(cfg.processed("predictions_future.parquet"))
        n = len(out)
        print(f"[predict] future city: mean P on today's links {out.p.mean():.3f} -> {fut.p.iloc[:n].mean():.3f}, "
              f"{len(fut) - n:,} links of new cells")
    return out


def _predict(cfg: Config, b: dict, X: pd.DataFrame, cells: pd.DataFrame, write_devices: bool = False) -> pd.DataFrame:
    feats = b["features"]
    geo = [f for f in feats if f not in DEVICE_FEATURES]
    mix = b.get("device_mix") or {"IMT": 1.0}
    flags = lambda m: {d: MAKERS.get(m, {}).get(d, 0.0) for d in DEVICE_FEATURES}  # noqa: E731
    # Links whose path leaves the surface model have no obstruction features (NaN). Almost no
    # training link is like that (0.1 %), so tree models handle NaN arbitrarily – CatBoost reads
    # it as "no obstruction at all". Use the distance + antenna-height model there instead.
    nogeo = X[GEOMETRY].isna().any(axis=1).to_numpy()
    Pf = rssi_f = None
    if nogeo.any() and b.get("fallback"):
        # clip to the training range: a linear model would extrapolate e.g. a 130 m mast without limit
        Xf = X.loc[nogeo, PHYSICS_COLS].copy()
        for c in PHYSICS_COLS:
            Xf[c] = Xf[c].clip(*b["train_ranges"][c])
        Pf = np.vstack([m.predict_proba(Xf)[:, 1] for m in b["fallback"]])
        rssi_f = b["rssi_fallback"].predict(X.loc[nogeo, ["log_d"]])
    # one prediction per meter manufacturer
    per = {}
    for m in mix:
        F = X[geo].assign(**flags(m))[feats]
        P_m = np.vstack([e.predict_proba(F)[:, 1] for e in b["ensemble"]])
        r_m = b["rssi"].predict(F)
        if Pf is not None:
            P_m[:, nogeo] = Pf[: len(P_m)]
            r_m[nogeo] = rssi_f
        per[m] = (P_m, r_m)
    wsum = sum(mix.values())
    P = sum(w * per[m][0] for m, w in mix.items()) / wsum      # today's meter mix
    rssi = sum(w * per[m][1] for m, w in mix.items()) / wsum
    weak = min(per, key=lambda m: per[m][0].mean())             # conservative planning basis
    out = X[["cell_idx", "site_idx"]].copy()
    out["p_raw"] = P.mean(0)
    # rule from "Ergänzende Bedingungen AVBWasserV": buildings > 40 m from the street -> meter shaft
    f = float(cfg.raw.get("meter_placement", {}).get("shaft_p_factor", 1.0))
    share = cells["shaft_share"].fillna(0).to_numpy()[X["cell_idx"].to_numpy()] if "shaft_share" in cells else 0.0
    shaft = 1 - share * (1 - f)
    out["p"] = out["p_raw"] * shaft
    out["p_std"] = P.std(0)
    out["p_weak"] = per[weak][0].mean(0) * shaft
    out["p_weak_std"] = per[weak][0].std(0)
    out["rssi"] = rssi
    out["no_geometry"] = nogeo
    devices = dict(mix=mix, weak=weak, mean_p={m: float(per[m][0].mean()) for m in per})
    if write_devices:
        cfg.processed("devices.json").write_text(json.dumps(devices, indent=1))
    # extrapolation: how far outside the training feature ranges is this link?
    lo = np.array([b["train_ranges"][f][0] for f in feats])
    hi = np.array([b["train_ranges"][f][1] for f in feats])
    v = X[geo].assign(**flags("IMT"))[feats].to_numpy()
    span = hi - lo
    use = span > 1e-6  # skip features that are (almost) constant in training, e.g. valid_frac
    with np.errstate(invalid="ignore"):
        ex = np.nanmax(np.clip(np.maximum(lo[use] - v[:, use], v[:, use] - hi[use]) / span[use], 0, None), axis=1)
    out["extrap"] = np.nan_to_num(ex, nan=0.0)
    out["valid_frac"] = X["valid_frac"].to_numpy()
    return out
