# analysis/ — the data-science layer

Everything in `pipeline/` is standard-library Python and runs the crawl. This
directory is the one place that needs more: `pip install -r requirements.txt`
(numpy, pandas, scikit-learn). It runs on a laptop after the pipeline, never on
the web host, and writes JSON the site renders as-is.

| module | does | writes |
|---|---|---|
| `county_models.py` | gradient-boosted trees + ridge for three measured county outcomes (medical debt in collections, premature mortality, years of life lost; CDC PLACES disease estimates are predictors only, never targets) from prices, insurance, income, race, rurality, access and behaviour (age and poverty join once the ACS source is fetched; one measure per fact — the PLACES uninsured rate and the white/people-of-colour complements are deliberately left out, see the module); cross-validated; permutation importance; partial dependence | `out/prices/models.json` |
| `hospital_models.py` | the hospital×item panel from mrf_charges: (1) a fair-price index per published file — two-way fixed effects in log space separate what the procedure costs everywhere from what this hospital charges across everything it sells — with a trees-plus-ridge second stage on what predicts the index; (2) estimated prices for basket items a file did not publish — low-rank matrix completion (iterative truncated SVD) on top of the fixed effects, rank chosen and uncertainty calibrated on held-out cells | `out/prices/hospital_models.json`, `out/prices/hospital_estimates.csv` |

Read the module docstring before changing a feature list: the wording of the
notes is copied onto the site verbatim, and the page's honesty depends on it.
