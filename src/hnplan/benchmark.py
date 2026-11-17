"""Model benchmark: which learner predicts reception best?

Every candidate is scored on exactly the same honest splits used by `train`:
  spatial – GroupKFold over 1 km blocks, repeated 3x with shuffled block assignment
  logo    – leave-one-gateway-out (the planning question: how does an UNSEEN site perform?)

Metrics (P(heard) is used as a probability by the optimiser, so calibration matters):
  auc       – ranking quality (1 = perfect, 0.5 = coin flip)
  logloss   – main criterion: rewards confident-and-right, punishes confident-and-wrong
  brier     – mean squared error of the probability
  acc / bacc – accuracy at P>0.5 and balanced accuracy. Plain accuracy is shown for
              completeness but is misleading here: only ~16 % of links are positive, so
              "never heard" already scores ~84 %.

Significance: paired bootstrap over meters (whole devices resampled) of the LOGO log-loss
difference against the current production model (`gbm`).

Run:  uv run --group bench hnplan benchmark     (LightGBM / XGBoost are optional extras)
"""
from __future__ import annotations

import json
import time
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import (ExtraTreesClassifier, HistGradientBoostingClassifier,
                              RandomForestClassifier)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, brier_score_loss, log_loss,
                             roc_auc_score)
from sklearn.model_selection import GroupKFold
from sklearn.neighbors import KNeighborsClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import SplineTransformer, StandardScaler
from sklearn.svm import SVC

from .config import Config
from .features import MONOTONIC
from .model import MODEL_FEATURES as FEATURES  # geometry features + meter manufacturer flags
from .model import PHYSICS_COLS, CatBoostMono, add_device_flags

REF = "gbm"  # previous production model (the reference for the significance test)


def _pre(*steps):
    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), *steps)


def candidates(seed: int) -> dict:
    mono = [MONOTONIC.get(f, 0) for f in FEATURES]
    m = {
        "physics": _pre(LogisticRegression(max_iter=2000)),
        "logreg": _pre(LogisticRegression(C=0.3, max_iter=5000)),
        "logreg_spline": _pre(SplineTransformer(n_knots=5, degree=3),
                              LogisticRegression(C=0.05, max_iter=5000)),
        "knn": _pre(KNeighborsClassifier(n_neighbors=40, weights="distance")),
        "svm_rbf": _pre(SVC(C=1.0, gamma="scale", probability=True, random_state=seed)),
        "mlp": _pre(MLPClassifier(hidden_layer_sizes=(64, 32), alpha=1e-3, max_iter=800,
                                  early_stopping=True, n_iter_no_change=25, random_state=seed)),
        "random_forest": make_pipeline(SimpleImputer(strategy="median"),
                                       RandomForestClassifier(n_estimators=500, min_samples_leaf=5,
                                                              max_features=0.4, n_jobs=-1, random_state=seed)),
        "extra_trees": make_pipeline(SimpleImputer(strategy="median"),
                                     ExtraTreesClassifier(n_estimators=500, min_samples_leaf=5,
                                                          max_features=0.5, n_jobs=-1, random_state=seed)),
        "gbm": HistGradientBoostingClassifier(learning_rate=0.05, max_iter=300, max_leaf_nodes=15,
                                              min_samples_leaf=20, l2_regularization=1.0,
                                              monotonic_cst=mono, random_state=seed),
        "gbm_unconstrained": HistGradientBoostingClassifier(learning_rate=0.05, max_iter=300, max_leaf_nodes=15,
                                                            min_samples_leaf=20, l2_regularization=1.0,
                                                            random_state=seed),
    }
    try:
        import lightgbm as lgb
        m["lightgbm"] = lgb.LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=15, min_child_samples=20,
                                           subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                                           monotone_constraints=mono, random_state=seed, verbose=-1, n_jobs=4)
    except ImportError:
        pass
    try:
        import xgboost as xgb
        m["xgboost"] = xgb.XGBClassifier(n_estimators=400, learning_rate=0.03, max_depth=4, min_child_weight=3,
                                         subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
                                         monotone_constraints=tuple(mono), random_state=seed, n_jobs=4,
                                         eval_metric="logloss")
    except ImportError:
        pass
    m["catboost"] = CatBoostMono(seed=seed)
    return m


def _cols(name):
    return PHYSICS_COLS if name == "physics" else FEATURES


def _metrics(y, p) -> dict:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return dict(auc=float(roc_auc_score(y, p)), logloss=float(log_loss(y, p, labels=[0, 1])),
                brier=float(brier_score_loss(y, p)), acc=float(accuracy_score(y, p > 0.5)),
                bacc=float(balanced_accuracy_score(y, p > 0.5)))


def _splits(L: pd.DataFrame, seed: int) -> dict:
    y = L["heard"].to_numpy()
    sp = [list(GroupKFold(n_splits=5, shuffle=True, random_state=seed + r).split(L, y, L["block"]))
          for r in range(3)]
    logo = list(GroupKFold(n_splits=L["site_id"].nunique()).split(L, y, L["site_id"]))
    return dict(spatial=sp, logo=[logo])


def _oof(est, X: pd.DataFrame, y, folds) -> np.ndarray:
    p = np.zeros(len(y))
    for tr, te in folds:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p[te] = clone(est).fit(X.iloc[tr], y[tr]).predict_proba(X.iloc[te])[:, 1]
    return p


def _boot_diff(y, pa, pb, groups, n=1000, seed=0) -> dict:
    """Paired bootstrap over meters: logloss(a) - logloss(b). Negative = a is better."""
    def ll(y, p):
        p = np.clip(p, 1e-4, 1 - 1e-4)
        return -(y * np.log(p) + (1 - y) * np.log(1 - p))
    d = ll(y, pa) - ll(y, pb)
    per = pd.Series(d).groupby(np.asarray(groups)).agg(["sum", "size"])
    s, c = per["sum"].to_numpy(), per["size"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(s), (n, len(s)))
    bs = s[idx].sum(1) / c[idx].sum(1)
    return dict(diff=float(d.mean()), ci_lo=float(np.percentile(bs, 2.5)), ci_hi=float(np.percentile(bs, 97.5)),
                p_better=float((bs < 0).mean()))


def benchmark(cfg: Config, only: list[str] | None = None) -> dict:
    seed = cfg.raw["model"]["random_state"]
    L = add_device_flags(pd.read_parquet(cfg.processed("train_links.parquet")))
    y = L["heard"].to_numpy()
    splits = _splits(L, seed)
    models = candidates(seed)
    if only:
        models = {k: v for k, v in models.items() if k in only or k == REF}
    print(f"[benchmark] {len(L):,} links, {y.mean():.1%} positive, {len(models)} models: {', '.join(models)}")

    rows, oof = [], {}
    for name, est in models.items():
        t0 = time.time()
        X = L[_cols(name)]
        r = dict(model=name)
        for scheme, reps in splits.items():
            ps = [_oof(est, X, y, folds) for folds in reps]
            ms = [_metrics(y, p) for p in ps]
            for k in ms[0]:
                r[f"{scheme}_{k}"] = float(np.mean([m[k] for m in ms]))
            oof[(scheme, name)] = np.mean(ps, axis=0)
        r["seconds"] = round(time.time() - t0, 1)
        rows.append(r)
        print(f"   {name:18s} spatial AUC {r['spatial_auc']:.3f} LL {r['spatial_logloss']:.4f} | "
              f"LOGO AUC {r['logo_auc']:.3f} LL {r['logo_logloss']:.4f} Brier {r['logo_brier']:.4f} "
              f"acc {r['logo_acc']:.3f}  [{r['seconds']}s]")

    # simple blend of the boosted-tree family (each OOF prediction never saw its own link)
    fam = [k for k in ("gbm", "lightgbm", "xgboost", "catboost") if k in models]
    if len(fam) >= 2:
        r = dict(model="blend_boosters", seconds=0.0)
        for scheme in splits:
            p = np.mean([oof[(scheme, k)] for k in fam], axis=0)
            oof[(scheme, "blend_boosters")] = p
            for k, v in _metrics(y, p).items():
                r[f"{scheme}_{k}"] = v
        rows.append(r)

    res = pd.DataFrame(rows).sort_values("logo_logloss").reset_index(drop=True)
    ref = oof[("logo", REF)]
    sig = {m: _boot_diff(y, oof[("logo", m)], ref, L["zaehlernummer_id"], seed=seed)
           for m in res.model if m != REF}
    res["vs_gbm_logloss"] = res.model.map(lambda m: sig[m]["diff"] if m in sig else 0.0)
    res["vs_gbm_ci"] = res.model.map(lambda m: f"[{sig[m]['ci_lo']:+.4f}, {sig[m]['ci_hi']:+.4f}]" if m in sig else "")
    res["p_better_than_gbm"] = res.model.map(lambda m: sig[m]["p_better"] if m in sig else np.nan)

    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print("\n[benchmark] ranked by leave-one-gateway-out log-loss (lower is better):")
        print(res[["model", "logo_auc", "logo_logloss", "logo_brier", "logo_acc", "logo_bacc",
                   "spatial_auc", "spatial_logloss", "vs_gbm_ci", "p_better_than_gbm"]].round(4).to_string(index=False))
    out = dict(n_links=int(len(L)), positive_rate=float(y.mean()), reference=REF,
               results=res.replace({np.nan: None}).to_dict("records"))
    cfg.processed("benchmark.json").write_text(json.dumps(out, indent=1))
    return out
