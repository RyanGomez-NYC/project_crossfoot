#!/usr/bin/env python3
"""
Hospital models: what each hospital charges once you account for what it sells.

    python3 -m analysis.hospital_models            # reads out/prices/*.csv
    python3 -m analysis.hospital_models --quick    # smaller matrix, fewer ranks

Writes out/prices/hospital_models.json (the model card, per-hospital price
index and everything a page needs to render it) and
out/prices/hospital_estimates.csv (estimated prices for basket items a
hospital did not publish). Runs on a laptop after the pipeline; never on the
web host. Requires numpy, pandas, scikit-learn (requirements.txt) — nothing
else, and all of it BSD-licensed.

What this is
------------
Two models on the hospital×item panel in mrf_charges (one row per hospital
file, code and setting, carrying the median negotiated rate the file
published).

1. A fair-price index. Median negotiated rates are decomposed, in log space,
   into an item effect (what the procedure costs everywhere) and a hospital
   effect (what this hospital charges across everything it sells) — a two-way
   fixed-effects model fitted by alternating means. The hospital effect is the
   index: 1.20 means this file's negotiated rates run 20% above what its own
   mix of items would predict at the average hospital. A second stage then
   asks what predicts the index (size, system, market) with the same
   trees-plus-ridge pair as county_models; the part no characteristic explains
   is the residual premium.

2. Price estimates for unpublished items. Each file publishes a fraction of
   the code universe, so the hospital×item matrix is mostly empty. On top of
   the fixed effects, a low-rank factorisation (iterative truncated SVD — the
   softImpute idea) learns the structure in who deviates on what, and fills
   the empty cells. Rank is chosen on held-out cells the model never saw, and
   the same held-out errors set the uncertainty band around every estimate.

What this is not
----------------
A statement about quality, cost to the hospital, or what any patient pays.
The unit is the published file, not the building: a system that publishes one
file per campus appears once per campus (rows carry the CCN so a reader can
group them). The negotiated median summarises whatever payers the file
listed — payer identity was not retained at parse time, so a hospital facing
generous payers and a hospital charging every payer more look the same here.
Estimates are model output, not published prices, and the site must say so
wherever they appear.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import KFold, cross_validate, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.utils.extmath import randomized_svd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "prices"
SEED = 20260821

# --- panel thresholds -------------------------------------------------------
# An item (code_type, code, setting) enters the fixed-effects panel when this
# many files price it, and a file enters when it prices this many items; the
# two filters are applied together until stable. Below these, an effect is a
# handful of numbers wearing a confidence interval.
MIN_FILES_PER_ITEM = 30
MIN_ITEMS_PER_FILE = 30

# The completion matrix is denser on purpose: imputation leans on co-observed
# cells, and a 2%-observed column contributes mostly noise. Items priced by
# at least this many files qualify, capped (by coverage) so the dense matrix
# stays laptop-sized.
MIN_FILES_PER_ITEM_MC = 100
MAX_ITEMS_MC = 20000

# Junk filter: within an item, a rate whose log10 sits more than 5 robust
# standard deviations from the item median is a placeholder ($1.00, $9999999)
# or a mis-parsed row, not a price. The floor stops a degenerate MAD of 0
# (many files quoting the identical rate) from flagging ordinary variation.
JUNK_Z = 5.0
MAD_FLOOR = 0.15  # log10 units, ≈ ×1.4

# Fraction of observed cells hidden from the completion model to choose the
# rank and calibrate the uncertainty band.
HOLDOUT_CELLS = 0.05
RANKS = [2, 5, 10, 20, 40]
RANKS_QUICK = [5, 10]

# --- stage-2 features -------------------------------------------------------
# key: column built in covariates(); value: (label, group, transform, unit) —
# same shape as county_models.FEATURES so the two pages can share a renderer.
FEATURES = {
    "beds":              ("Beds (AHRQ compendium)", "hospital", "log", ""),
    "in_system":         ("Belongs to a health system", "hospital", None, "0/1"),
    "system_hospitals":  ("Hospitals in its system", "hospital", "log", ""),
    "ruca":              ("Location rurality (RUCA 1–10)", "market", None, ""),
    "median_household_income": ("County median household income", "market", "log", "$"),
    "uninsured_pct":     ("County uninsured share (under 65)", "market", None, "%"),
    "rural_pct":         ("County rural share", "market", None, "%"),
    "population":        ("County population", "market", "log", ""),
    "income_inequality": ("County income inequality (80/20)", "market", None, "×"),
    "items_n":           ("Items the file prices", "file", "log", ""),
}

# Not used, and why — so the omission is a decision rather than an accident:
#   ownership            hospitals.csv carries the AHRQ hos_ownership code, but
#                        five values arrive without their codebook labels and a
#                        third of hospitals arrive without a value. A dummy per
#                        unexplained digit would put numbers on the page nobody
#                        can read. Joins once the codebook is pinned as a source.
#   state                fifty dummies would soak up every regional pattern the
#                        market features are there to explain, and the page
#                        already shows the index on a map by state.
#   charge_to_payment    the system-level Medicare billing ratio is computed
#                        from charges, so putting it on the right-hand side of
#                        a price model explains prices with prices.

MIN_COVERAGE = 0.25  # a stage-2 feature is used when this share of files has it

NOTES = {
    "index": "Fair-price index: median negotiated rates decomposed, in log space, into an item effect "
             "(what the procedure costs everywhere) and a hospital effect (what this file charges across "
             "everything it prices) — a two-way fixed-effects model fitted by alternating means. 1.20 means "
             "20% above what the file's own mix of items would predict at the average hospital. The interval "
             "is ±1.96 standard errors from the within-file spread of residuals; residuals of one file are "
             "treated as independent, so read it as approximate.",
    "stage2": "What predicts the index: gradient-boosted trees (scikit-learn HistGradientBoostingRegressor) "
              "and ridge regression on standardised features, 5-fold cross-validation, importances on a 25% "
              "hold-out — the same pair, and the same caveats, as the county page. The residual is the premium "
              "no listed characteristic explains. Nothing here establishes cause.",
    "completion": "Estimates for unpublished items: on top of the fixed effects, a low-rank factorisation "
                  "(iterative truncated SVD, the softImpute idea) learns the structure in who deviates on "
                  "what and fills the empty cells. The rank is chosen on held-out cells the model never saw; "
                  "the band around each estimate is the 10th–90th percentile of the model's own held-out "
                  "errors. An estimate is model output, not a published price.",
    "unit": "The unit is the published file, not the building. A system that publishes one file per campus "
            "appears once per campus; rows carry the CCN so files for one hospital can be grouped. The "
            "negotiated median summarises whatever payers the file listed — payer identity is not in the "
            "data, so payer mix and pricing posture cannot be told apart.",
    "junk": "Within an item, a rate more than 5 robust standard deviations (log scale) from the item median "
            "is treated as a placeholder or parse error and set aside, with the count reported here.",
}


def log(msg: str) -> None:
    print(msg, flush=True)


# --- data -------------------------------------------------------------------

def load_panel(d: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The charge panel (one row per file×item, junk removed) and file metadata."""
    charges = pd.read_csv(
        d / "mrf_charges.csv",
        usecols=["seed_id", "code_type", "code", "setting", "negotiated_median"],
        dtype={"seed_id": "category", "code_type": "category", "code": str, "setting": "category"},
    )
    n_raw = len(charges)
    charges = charges[charges["negotiated_median"] > 0].copy()

    # a price for a non-code is not a price — same rule as the catalog
    codes = pd.read_csv(d / "codes.csv", usecols=["code_type", "code", "status"],
                        dtype={"code_type": str, "code": str, "status": str})
    ok = codes[codes["status"].isin(["official", "hospital_only"])]
    charges["code_type"] = charges["code_type"].astype(str)
    charges = charges.merge(ok[["code_type", "code"]], on=["code_type", "code"], how="inner")

    charges["item"] = (charges["code_type"] + "|" + charges["code"] + "|"
                       + charges["setting"].astype(str))
    charges["y"] = np.log10(charges["negotiated_median"].astype(float))

    # junk filter, per item
    g = charges.groupby("item")["y"]
    med = g.transform("median")
    mad = (charges["y"] - med).abs().groupby(charges["item"]).transform("median")
    sigma = np.maximum(1.4826 * mad, MAD_FLOOR)
    junk = (charges["y"] - med).abs() / sigma > JUNK_Z
    n_junk = int(junk.sum())
    charges = charges[~junk]

    # co-filter thin items and thin files until stable
    for _ in range(4):
        before = len(charges)
        keep_i = charges["item"].value_counts()
        charges = charges[charges["item"].isin(keep_i.index[keep_i >= MIN_FILES_PER_ITEM])]
        keep_f = charges["seed_id"].value_counts()
        charges = charges[charges["seed_id"].isin(keep_f.index[keep_f >= MIN_ITEMS_PER_FILE])]
        if len(charges) == before:
            break

    files = pd.read_csv(d / "mrf_files.csv",
                        usecols=["seed_id", "state", "hospital", "ccn", "status"], dtype=str)
    log(f"  panel: {len(charges):,} of {n_raw:,} rows after filters "
        f"({n_junk:,} junk); {charges['seed_id'].nunique():,} files × "
        f"{charges['item'].nunique():,} items")
    return charges.reset_index(drop=True), files


def two_way_fe(h: np.ndarray, i: np.ndarray, y: np.ndarray,
               n_h: int, n_i: int) -> tuple[float, np.ndarray, np.ndarray]:
    """mu, alpha (per hospital), beta (per item) by alternating means."""
    mu = float(y.mean())
    alpha = np.zeros(n_h)
    beta = np.zeros(n_i)
    cnt_h = np.bincount(h, minlength=n_h).astype(float)
    cnt_i = np.bincount(i, minlength=n_i).astype(float)
    for _ in range(200):
        beta = np.bincount(i, weights=y - mu - alpha[h], minlength=n_i) / cnt_i
        new_alpha = np.bincount(h, weights=y - mu - beta[i], minlength=n_h) / cnt_h
        shift = float(new_alpha.mean())     # keep alpha centred on the average file
        mu += shift
        new_alpha -= shift
        delta = float(np.abs(new_alpha - alpha).max())
        alpha = new_alpha
        if delta < 1e-9:
            break
    return mu, alpha, beta


# --- stage 2: what predicts the index ---------------------------------------

def covariates(d: Path, files: pd.DataFrame, per_file_items: pd.Series) -> pd.DataFrame:
    hosp = pd.read_csv(d / "hospitals.csv",
                       usecols=["ccn", "county_fips", "ruca", "beds", "system_id"], dtype=str)
    systems = pd.read_csv(d / "systems.csv", usecols=["system_id", "hospitals_n"], dtype=str)
    county = pd.read_csv(d / "county_profile.csv",
                         usecols=["fips", "level", "median_household_income", "uninsured_pct",
                                  "rural_pct", "population", "income_inequality"], dtype=str)
    county = county[county["level"] == "county"].rename(columns={"fips": "county_fips"})

    cv = (files.merge(hosp, on="ccn", how="left")
               .merge(systems, on="system_id", how="left")
               .merge(county.drop(columns=["level"]), on="county_fips", how="left"))
    cv["beds"] = pd.to_numeric(cv["beds"], errors="coerce")
    cv["ruca"] = pd.to_numeric(cv["ruca"], errors="coerce")
    cv["in_system"] = cv["system_id"].notna().astype(float)
    cv["system_hospitals"] = pd.to_numeric(cv["hospitals_n"], errors="coerce").fillna(1.0)
    for c in ["median_household_income", "uninsured_pct", "rural_pct", "population", "income_inequality"]:
        cv[c] = pd.to_numeric(cv[c], errors="coerce")
    cv["items_n"] = cv["seed_id"].map(per_file_items).astype(float)
    return cv


def fit_stage2(cv: pd.DataFrame, quick: bool) -> tuple[dict, pd.Series]:
    feats = [f for f in FEATURES if cv[f].notna().mean() >= MIN_COVERAGE]
    X = cv[feats].astype(float).copy()
    for f in feats:
        if FEATURES[f][2] == "log":
            X[f] = np.log10(X[f].where(X[f] > 0))
    y = cv["alpha"].astype(float)

    gbr = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_depth=4,
                                        early_stopping=True, validation_fraction=0.15,
                                        random_state=SEED)
    folds = 3 if quick else 5
    kf = KFold(n_splits=folds, shuffle=True, random_state=SEED)
    scores = cross_validate(gbr, X, y, cv=kf, scoring=("r2", "neg_mean_absolute_error"), n_jobs=1)
    Xtr, Xho, ytr, yho = train_test_split(X, y, test_size=0.25, random_state=SEED)
    gbr.fit(Xtr, ytr)
    perm = permutation_importance(gbr, Xho, yho, n_repeats=3 if quick else 10,
                                  random_state=SEED, scoring="r2", n_jobs=1)
    ridge = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                          RidgeCV(alphas=np.logspace(-2, 3, 20)))
    ridge.fit(Xtr, ytr)
    coefs = ridge[-1].coef_ / (ytr.std() or 1.0)

    order = np.argsort(-perm.importances_mean)
    features_out = [{
        "feature": feats[i], "label": FEATURES[feats[i]][0], "group": FEATURES[feats[i]][1],
        "unit": FEATURES[feats[i]][3], "rank": r,
        "importance": round(float(perm.importances_mean[i]), 5),
        "importance_sd": round(float(perm.importances_std[i]), 5),
        "ridge_coef_std": round(float(coefs[i]), 4),
        "coverage": round(float(X[feats[i]].notna().mean()), 3),
    } for r, i in enumerate(order, 1)]

    # predicted index for every file, from a model fitted on the training split
    predicted = pd.Series(gbr.predict(X), index=cv.index)
    return {
        "n": int(len(y)), "features_n": len(feats), "features": feats,
        "cv_folds": folds,
        "cv_r2": round(float(scores["test_r2"].mean()), 4),
        "cv_r2_sd": round(float(scores["test_r2"].std()), 4),
        "cv_mae_log10": round(float(-scores["test_neg_mean_absolute_error"].mean()), 4),
        "holdout_r2": round(float(gbr.score(Xho, yho)), 4),
        "ridge_holdout_r2": round(float(ridge.score(Xho, yho)), 4),
        "ridge_alpha": round(float(ridge[-1].alpha_), 4),
        "feature_importance": features_out,
    }, predicted


# --- matrix completion ------------------------------------------------------

def soft_impute(R: np.ndarray, mask: np.ndarray, rank: int,
                iters: int = 30, tol: float = 1e-4) -> np.ndarray:
    """Fill the False cells of mask by iterative truncated SVD of R (softImpute
    with hard rank truncation). R's observed cells are residuals, so 0 is the
    natural starting value for the missing ones."""
    X = np.where(mask, R, 0.0)
    prev = None
    for _ in range(iters):
        U, s, Vt = randomized_svd(X, n_components=rank, random_state=SEED)
        Xhat = (U * s) @ Vt
        X = np.where(mask, R, Xhat)
        change = float(np.abs(Xhat - prev).mean()) if prev is not None else np.inf
        prev = Xhat
        if change < tol:
            break
    return Xhat


def fit_completion(panel: pd.DataFrame, quick: bool, rng: np.random.Generator) -> tuple[dict, dict]:
    """Choose a rank on held-out cells, refit on everything, and return the
    pieces needed to estimate any cell: mu, alpha, beta, low-rank layer."""
    counts = panel["item"].value_counts()
    dense = counts[counts >= MIN_FILES_PER_ITEM_MC]
    if len(dense) > MAX_ITEMS_MC:
        dense = dense.iloc[:MAX_ITEMS_MC]
    sub = panel[panel["item"].isin(dense.index)].copy()

    h_codes, h_idx = pd.factorize(sub["seed_id"].astype(str))
    i_codes, i_idx = pd.factorize(sub["item"])
    y = sub["y"].to_numpy(dtype=np.float64)
    n_h, n_i = len(h_idx), len(i_idx)
    log(f"  completion matrix: {n_h:,} files × {n_i:,} items, "
        f"{len(sub) / (n_h * n_i):.1%} observed")

    hide = rng.random(len(sub)) < HOLDOUT_CELLS
    tr = ~hide

    # fixed effects on training cells only — held-out cells stay unseen
    mu_t, alpha_t, beta_t = two_way_fe(h_codes[tr], i_codes[tr], y[tr], n_h, n_i)
    base = mu_t + alpha_t[h_codes] + beta_t[i_codes]
    resid = y - base

    R = np.zeros((n_h, n_i), dtype=np.float64)
    mask = np.zeros((n_h, n_i), dtype=bool)
    R[h_codes[tr], i_codes[tr]] = resid[tr]
    mask[h_codes[tr], i_codes[tr]] = True

    ho_true = y[hide]
    ho_base = base[hide]
    var_ho = float(((ho_true - ho_true.mean()) ** 2).mean())

    def score(pred: np.ndarray) -> tuple[float, float]:
        err = ho_true - pred
        r2 = 1.0 - float((err ** 2).mean()) / var_ho
        med_pct = float(10 ** np.median(np.abs(err)) - 1)  # median |error| as a price ratio
        return round(r2, 4), round(med_pct, 4)

    validation = [{"rank": 0, "holdout_r2": score(ho_base)[0],
                   "median_abs_err_pct": round(score(ho_base)[1] * 100, 2)}]
    best = (0, validation[0]["holdout_r2"], None)
    for rank in (RANKS_QUICK if quick else RANKS):
        Xhat = soft_impute(R, mask, rank)
        pred = ho_base + Xhat[h_codes[hide], i_codes[hide]]
        r2, med_pct = score(pred)
        validation.append({"rank": rank, "holdout_r2": r2,
                           "median_abs_err_pct": round(med_pct * 100, 2)})
        log(f"    rank {rank:>3}: held-out R² {r2:.4f}, median abs error {med_pct:.1%}")
        if r2 > best[1]:
            err = np.abs(ho_true - pred)
            best = (rank, r2, {"q50": float(np.quantile(err, 0.50)),
                               "q90": float(np.quantile(err, 0.90))})

    # refit on every observed cell at the chosen rank
    mu, alpha, beta = two_way_fe(h_codes, i_codes, y, n_h, n_i)
    R[h_codes, i_codes] = y - (mu + alpha[h_codes] + beta[i_codes])
    mask[h_codes, i_codes] = True
    Xhat = soft_impute(R, mask, best[0]) if best[0] else np.zeros_like(R)

    result = {"rank": best[0], "holdout_r2": best[1],
              "holdout_cells": int(hide.sum()), "validation": validation,
              "err_q50_log10": round(best[2]["q50"], 4) if best[2] else None,
              "err_q90_log10": round(best[2]["q90"], 4) if best[2] else None}
    pieces = {"mu": mu, "alpha": alpha, "beta": beta, "lowrank": Xhat,
              "files": h_idx, "items": i_idx, "mask": mask,
              "err_q90": best[2]["q90"] if best[2] else 0.30}
    return result, pieces


def write_estimates(d: Path, pieces: dict, files: pd.DataFrame, out_csv: Path) -> int:
    """Estimated prices for basket items a file did not publish."""
    basket = pd.read_csv(d / "state_basket.csv",
                         usecols=["code_type", "code", "label", "group"],
                         dtype=str).drop_duplicates(["code_type", "code"])
    item_pos = {it: k for k, it in enumerate(pieces["items"])}
    meta = files.set_index("seed_id")[["hospital", "state", "ccn"]]
    q90 = pieces["err_q90"]

    rows = []
    for _, b in basket.iterrows():
        for setting in ("inpatient", "outpatient", "both"):
            it = f"{b.code_type}|{b.code}|{setting}"
            k = item_pos.get(it)
            if k is None:
                continue
            pred_log = (pieces["mu"] + pieces["alpha"] + pieces["beta"][k] + pieces["lowrank"][:, k])
            missing = ~pieces["mask"][:, k]
            for hidx in np.flatnonzero(missing):
                sid = pieces["files"][hidx]
                m = meta.loc[sid] if sid in meta.index else None
                est = 10 ** pred_log[hidx]
                rows.append({
                    "seed_id": sid,
                    "ccn": (m["ccn"] if m is not None else None),
                    "hospital": (m["hospital"] if m is not None else None),
                    "state": (m["state"] if m is not None else None),
                    "code_type": b.code_type, "code": b.code, "setting": setting,
                    "basket_label": b.label, "basket_group": b.group,
                    "est_usd": round(est, 2),
                    "lo90_usd": round(est / 10 ** q90, 2),
                    "hi90_usd": round(est * 10 ** q90, 2),
                })
    out = pd.DataFrame(rows)
    out.to_csv(out_csv, index=False)
    return len(out)


# --- main -------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--dir", default=str(OUT), help="directory with the pipeline's csv exports")
    ap.add_argument("--output", default=str(OUT / "hospital_models.json"))
    ap.add_argument("--estimates", default=str(OUT / "hospital_estimates.csv"))
    args = ap.parse_args(argv)
    d = Path(args.dir)
    t0 = time.time()
    rng = np.random.default_rng(SEED)

    log("panel")
    panel, files = load_panel(d)

    log("fair-price index (two-way fixed effects)")
    h_codes, h_idx = pd.factorize(panel["seed_id"].astype(str))
    i_codes, i_idx = pd.factorize(panel["item"])
    y = panel["y"].to_numpy(dtype=np.float64)
    mu, alpha, beta = two_way_fe(h_codes, i_codes, y, len(h_idx), len(i_idx))
    resid = y - mu - alpha[h_codes] - beta[i_codes]
    fe_r2 = 1.0 - float((resid ** 2).mean()) / float(((y - y.mean()) ** 2).mean())
    n_items_file = np.bincount(h_codes)
    se = np.sqrt(np.bincount(h_codes, weights=resid ** 2) / n_items_file) / np.sqrt(n_items_file)
    log(f"  mu={mu:.3f} (≈ ${10**mu:,.0f} typical item)  R²={fe_r2:.3f}  "
        f"index spread ×{10**alpha.min():.2f}–×{10**alpha.max():.2f}")

    log("stage 2: what predicts the index")
    cv = covariates(d, files, pd.Series(n_items_file, index=h_idx))
    cv = cv.set_index("seed_id").reindex(h_idx).reset_index().rename(columns={"index": "seed_id"})
    cv["alpha"] = alpha
    stage2, predicted = fit_stage2(cv, args.quick)
    log(f"  n={stage2['n']:,}  cv R² {stage2['cv_r2']:.3f} ± {stage2['cv_r2_sd']:.3f}  "
        f"holdout R² {stage2['holdout_r2']:.3f}  ridge {stage2['ridge_holdout_r2']:.3f}  "
        f"top: {', '.join(x['label'] for x in stage2['feature_importance'][:3])}")

    log("matrix completion")
    completion, pieces = fit_completion(panel, args.quick, rng)
    n_est = write_estimates(d, pieces, files, Path(args.estimates))
    log(f"  {n_est:,} basket estimates → {args.estimates}")

    hospitals_out = []
    for k, sid in enumerate(h_idx):
        f = files[files["seed_id"] == sid]
        f = f.iloc[0] if len(f) else None
        lo, hi = 10 ** (alpha[k] - 1.96 * se[k]), 10 ** (alpha[k] + 1.96 * se[k])
        hospitals_out.append({
            "seed_id": sid,
            "ccn": (None if f is None or pd.isna(f["ccn"]) else f["ccn"]),
            "hospital": (None if f is None else f["hospital"]),
            "state": (None if f is None else f["state"]),
            "items_n": int(n_items_file[k]),
            "index": round(float(10 ** alpha[k]), 4),
            "index_lo": round(float(lo), 4),
            "index_hi": round(float(hi), 4),
            "predicted_index": round(float(10 ** predicted.iloc[k]), 4),
            "residual_index": round(float(10 ** (alpha[k] - predicted.iloc[k])), 4),
        })
    hospitals_out.sort(key=lambda r: -r["index"])

    out = {
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": SEED,
        "library": {"sklearn": __import__("sklearn").__version__,
                    "numpy": np.__version__, "pandas": pd.__version__},
        "notes": NOTES,
        "feature_catalog": {k: {"label": v[0], "group": v[1], "transform": v[2], "unit": v[3]}
                            for k, v in FEATURES.items()},
        "panel": {
            "files_n": int(len(h_idx)), "items_n": int(len(i_idx)),
            "cells_n": int(len(panel)),
            "min_files_per_item": MIN_FILES_PER_ITEM, "min_items_per_file": MIN_ITEMS_PER_FILE,
            "fe_r2": round(fe_r2, 4),
            "mu_log10": round(mu, 4),
        },
        "stage2": stage2,
        "completion": completion,
        "hospitals": hospitals_out,
    }
    Path(args.output).write_text(json.dumps(out, indent=1))
    log(f"\nhospital_models → {args.output}  ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
