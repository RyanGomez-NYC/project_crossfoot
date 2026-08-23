#!/usr/bin/env python3
"""
County models: what predicts medical debt, premature death and disease rates.

    python3 -m analysis.county_models            # reads out/prices/county_profile.json
    python3 -m analysis.county_models --quick    # fewer folds and repeats, for a smoke test

Writes out/prices/models.json, which tools/export_prices_sql.py in the web
repository turns into the model_run / model_feature / model_pd tables that the
site's Drivers page renders. Every number on that page comes from this file.

What this is
------------
One year of cross-sectional data on ~3,100 counties. For each target we fit two
models on the same features:

  * a gradient-boosted tree ensemble (HistGradientBoostingRegressor), which
    captures non-linear and interacting relationships and handles missing
    values natively, evaluated by 5-fold cross-validation;
  * a ridge regression on standardised features, which gives a signed,
    comparable coefficient per feature and a sanity check on the trees.

From the tree model we report permutation importance (how much held-out R²
falls when a feature is shuffled — a measure of reliance, not of effect size)
and partial dependence (the model's average prediction as one feature moves
across its range with the others held at their observed values).

What this is not
----------------
Causal inference. The data cannot separate "prices raise debt" from "places
with high debt have hospitals that set high prices" from "both follow from
income". Partial dependence shows what the model learned, not what would
happen if a factor changed. The site says so next to every chart, and the
wording in LABELS and NOTES below is written to be copied there verbatim.

Requires numpy, pandas, scikit-learn (requirements.txt). Runs on a laptop in a
minute or two; never on the web host.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import partial_dependence, permutation_importance
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import KFold, cross_validate, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "prices"
SEED = 20260821

# --- features ---------------------------------------------------------------
# key: column in county_profile; value: (label, group, transform, unit)
# transform 'log' means log10 is applied before modelling (and the partial
# dependence grid is reported back in original units).
FEATURES = {
    "inpatient_charge_to_payment":  ("Hospitals bill per $1 paid (inpatient)", "prices", None, "×"),
    "outpatient_charge_to_allowed": ("Hospitals bill per $1 allowed (outpatient)", "prices", None, "×"),
    "drg470_avg_charge":            ("Joint-replacement charge (DRG 470)", "prices", "log", "$"),
    "uninsured_pct":                ("Uninsured share (under 65)", "insurance", None, "%"),
    "median_household_income":      ("Median household income", "economy", "log", "$"),
    "poverty_pct":                  ("Below poverty line", "economy", None, "%"),
    "unemployment_pct":             ("Unemployment rate", "economy", None, "%"),
    "severe_housing_pct":           ("Severe housing problems", "economy", None, "%"),
    "food_insecurity_pct":          ("Food-insecure share", "economy", None, "%"),
    "median_age":                   ("Median age", "age", None, "yrs"),
    "age65_pct":                    ("Aged 65+", "age", None, "%"),
    "black_pct":                    ("Non-Hispanic Black share", "demographics", None, "%"),
    "hispanic_pct":                 ("Hispanic share", "demographics", None, "%"),
    "rural_pct":                    ("Rural share", "demographics", None, "%"),
    "population":                   ("Population", "demographics", "log", ""),
    "pcp_ratio":                    ("Residents per primary-care physician", "access", "log", ""),
    "mhp_ratio":                    ("Residents per mental-health provider", "access", "log", ""),
    "hospitals_n":                  ("Hospitals in county (Medicare files)", "access", None, ""),
    "checkup_pct":                  ("Adults with a routine checkup in the past year", "access", None, "%"),
    "smoking_pct":                  ("Adults who smoke", "behaviour", None, "%"),
    "obesity_pct":                  ("Adults with obesity", "behaviour", None, "%"),
}

# features that are themselves CDC PLACES model-based estimates
PLACES_FEATURES = ["checkup_pct", "smoking_pct", "obesity_pct"]

# Not used, and why — so the omission is a decision rather than an accident:
#   no_insurance_18_64_pct  PLACES' uninsured rate is a small-area estimate built
#                           from ACS covariates; uninsured_pct (ACS, via CHR) is the
#                           measured figure and correlates 0.77 with it. Two
#                           versions of one fact would split the credit and put
#                           "uninsured" on the page twice.
#   white_pct, people_of_color_pct   Black + Hispanic + white shares sum to ~100%,
#                           so with all three in a linear model the coefficients
#                           are arbitrary (the ridge put -0.75 on "white" and
#                           -0.32 on "Black" for YPLL, opposite to the data).
#                           Keep the two shares that vary independently; the
#                           white share is their complement, and Urban's
#                           people_of_color_pct is the same complement (r = -1.00).
#   drg470_avg_charge       present for 20% of counties; below MIN_COVERAGE.
#   median_age, poverty_pct, age65_pct   ACS columns; empty until the ACS
#                           source is fetched (python3 -m pipeline.prices.fetch acs).

# a feature is used when at least this share of counties has a value. The
# trees handle the remaining gaps natively (a missing value takes its own
# branch); the ridge median-imputes. The Medicare price ratios exist only for
# the ~1,350 counties with a hospital in the Medicare files (43%), so the
# threshold sits below that on purpose: the page exists to ask whether prices
# predict debt, and a model without price features cannot answer it.
MIN_COVERAGE = 0.25

# target: (label, unit, features to EXCLUDE because they are the outcome itself
# or a near-restatement of it)
TARGETS = {
    "medical_debt_pct":       ("Share of adults with medical debt in collections", "%", []),
    "premature_mortality_aa": ("Premature deaths per 100,000, age-adjusted", "", []),
    "premature_death_ypll":   ("Years of potential life lost per 100,000", "", []),
    # PLACES disease rates are not modelled as targets. CDC produces them by
    # small-area estimation from BRFSS with ACS age, sex, race and poverty as
    # the covariates — so "predicting" a PLACES rate from income and race
    # largely recovers CDC's own model (R² 0.86–0.94 here), which would be
    # presented as a finding about disease when it is a finding about the
    # estimator. Targets are the three measured outcomes: debt in collections
    # (credit records) and the two mortality measures (death certificates).
}

NOTES = {
    "method": "Gradient-boosted trees (scikit-learn HistGradientBoostingRegressor, 300 trees, "
              "learning rate 0.05, max depth 4, early stopping) and ridge regression on standardised "
              "features. Counties with a missing target are dropped; a feature is used when at least a "
              "quarter of counties have it, and its remaining gaps are handled natively by the trees (a "
              "missing value takes its own branch) and median-imputed for the ridge. 5-fold cross-validation; "
              "importances and partial dependence computed on a 25% hold-out the trees never saw.",
    "importance": "Permutation importance: the fall in hold-out R² when that one feature is randomly "
                  "shuffled, averaged over 10 shuffles. It measures how much the model relies on the "
                  "feature, not how large its effect is, and correlated features share credit.",
    "pd": "Partial dependence: the model's average prediction as this feature is set to each value on "
          "the x-axis for every county, with the other features left as observed. It shows what the "
          "model learned, not what would happen if the feature changed.",
    "ridge": "Ridge coefficient: change in the target, in standard deviations, per one standard "
             "deviation of the feature, holding the others constant in a linear model. Sign is the "
             "direction; size is comparable across features. Regularisation strength chosen by CV.",
    "limits": "One year, cross-sectional, county level. Nothing here establishes cause. Seven states "
              "bar medical debt from credit reports and are absent from the debt model. CDC PLACES "
              "disease rates are not modelled as outcomes: they are themselves estimated from the same "
              "demographic covariates, so a model of them would mostly recover CDC's model. PLACES "
              "smoking, obesity and checkup rates are used as predictors, with that caveat; its uninsured rate is "
              "not, because the ACS figure measures the same thing directly. Medicare price ratios exist "
              "only for counties with a hospital in the Medicare files (43%); elsewhere the trees see a gap. "
              "Age and poverty are absent from this build (the ACS source was not fetched).",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def prepare(df: pd.DataFrame, target: str, exclude: list[str]) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    feats = [f for f in FEATURES if f in df.columns and f != target and f not in exclude]
    # drop a feature nobody has (e.g. PLACES not fetched yet) rather than fit on NaNs
    feats = [f for f in feats if df[f].notna().mean() >= MIN_COVERAGE]
    X = df[feats].astype(float).copy()
    for f in feats:
        if FEATURES[f][2] == "log":
            X[f] = np.log10(X[f].where(X[f] > 0))
    y = df[target].astype(float)
    keep = y.notna()
    return X[keep], y[keep], feats


def fit_target(df: pd.DataFrame, target: str, quick: bool) -> dict | None:
    label, unit, exclude = TARGETS[target]
    if target not in df.columns or df[target].notna().sum() < 200:
        log(f"  {target}: not enough counties with a value; skipped")
        return None
    X, y, feats = prepare(df, target, exclude)
    n = len(y)
    dropped = {f: (0.0 if f not in df.columns else round(float(df[f].notna().mean()), 3))
               for f in FEATURES if f not in feats and f != target and f not in exclude}
    log(f"  {target}: n={n:,} features={len(feats)}" + (f"  dropped (coverage<{MIN_COVERAGE}): {dropped}" if dropped else ""))

    gbr = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_depth=4,
                                        early_stopping=True, validation_fraction=0.15,
                                        random_state=SEED)
    folds = 3 if quick else 5
    cv = KFold(n_splits=folds, shuffle=True, random_state=SEED)
    scores = cross_validate(gbr, X, y, cv=cv, scoring=("r2", "neg_mean_absolute_error"), n_jobs=1)
    cv_r2, cv_r2_sd = float(scores["test_r2"].mean()), float(scores["test_r2"].std())
    cv_mae = float(-scores["test_neg_mean_absolute_error"].mean())

    Xtr, Xho, ytr, yho = train_test_split(X, y, test_size=0.25, random_state=SEED)
    gbr.fit(Xtr, ytr)
    ho_r2 = float(gbr.score(Xho, yho))
    perm = permutation_importance(gbr, Xho, yho, n_repeats=3 if quick else 10,
                                  random_state=SEED, scoring="r2", n_jobs=1)

    ridge = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                          RidgeCV(alphas=np.logspace(-2, 3, 20)))
    ridge.fit(Xtr, ytr)
    ridge_r2 = float(ridge.score(Xho, yho))
    coefs = ridge[-1].coef_ / (ytr.std() or 1.0)   # standardised: SD of y per SD of x

    order = np.argsort(-perm.importances_mean)
    features_out = []
    for rank, i in enumerate(order, 1):
        f = feats[i]
        features_out.append({
            "feature": f, "label": FEATURES[f][0], "group": FEATURES[f][1], "unit": FEATURES[f][3],
            "rank": rank,
            "importance": round(float(perm.importances_mean[i]), 5),
            "importance_sd": round(float(perm.importances_std[i]), 5),
            "ridge_coef_std": round(float(coefs[i]), 4),
            "coverage": round(float(X[f].notna().mean()), 3),
        })

    pd_out = []
    top = [feats[i] for i in order[:8]]
    for f in top:
        i = feats.index(f)
        col = Xho[f].to_numpy(dtype=float)
        col = col[np.isfinite(col)]
        if len(col) < 20:
            continue
        # grid on the 2nd–98th percentiles of the observed (non-missing) values,
        # so a handful of extreme counties cannot stretch the axis
        grid = np.unique(np.linspace(np.percentile(col, 2), np.percentile(col, 98), 20))
        try:
            # method='brute' averages real predictions; 'recursion' (the default
            # for this estimator) returns values offset by a constant, which is
            # useless on an axis labelled in the target's own units
            res = partial_dependence(gbr, Xho, features=[i], custom_values={i: grid},
                                     kind="average", method="brute")
        except Exception as e:  # noqa: BLE001
            log(f"    pd {f}: {e}")
            continue
        avg = res["average"][0]
        for gx, gy in zip(grid, avg):
            if not (math.isfinite(float(gx)) and math.isfinite(float(gy))):
                continue
            x = float(gx)
            if FEATURES[f][2] == "log":
                x = float(10 ** x)
            pd_out.append({"feature": f, "x": round(x, 4), "y": round(float(gy), 4)})

    return {
        "target": target, "label": label, "unit": unit,
        "n": int(n), "n_train": int(len(ytr)), "n_holdout": int(len(yho)),
        "features_n": len(feats), "features": feats,
        "cv_folds": folds, "cv_r2": round(cv_r2, 4), "cv_r2_sd": round(cv_r2_sd, 4), "cv_mae": round(cv_mae, 4),
        "holdout_r2": round(ho_r2, 4), "ridge_holdout_r2": round(ridge_r2, 4),
        "ridge_alpha": round(float(ridge[-1].alpha_), 4),
        "target_mean": round(float(y.mean()), 4), "target_sd": round(float(y.std()), 4),
        "excluded": exclude,
        "dropped_low_coverage": dropped,
        "feature_importance": features_out,
        "partial_dependence": pd_out,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--input", default=str(OUT / "county_profile.json"))
    ap.add_argument("--output", default=str(OUT / "models.json"))
    args = ap.parse_args(argv)

    t0 = time.time()
    rows = json.loads(Path(args.input).read_text())
    df = pd.DataFrame([r for r in rows if r.get("level") == "county"])
    log(f"{len(df):,} counties; {sum(1 for f in FEATURES if f in df.columns)} of {len(FEATURES)} features present")

    runs = []
    for target in TARGETS:
        r = fit_target(df, target, args.quick)
        if r:
            runs.append(r)
            log(f"    cv R² {r['cv_r2']:.3f} ± {r['cv_r2_sd']:.3f}  holdout R² {r['holdout_r2']:.3f}  "
                f"ridge {r['ridge_holdout_r2']:.3f}  top: {', '.join(x['label'] for x in r['feature_importance'][:3])}")

    out = {
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": SEED,
        "library": {"sklearn": __import__("sklearn").__version__, "numpy": np.__version__, "pandas": pd.__version__},
        "notes": NOTES,
        "feature_catalog": {k: {"label": v[0], "group": v[1], "transform": v[2], "unit": v[3]} for k, v in FEATURES.items()},
        "runs": runs,
    }
    Path(args.output).write_text(json.dumps(out, indent=1))
    log(f"\n{len(runs)} models → {args.output}  ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
