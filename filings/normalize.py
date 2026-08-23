"""
Normalize raw CMS-0057-F prior authorization filings into one flat schema.

Every output row carries its source_url. Percentages are computed from counts
wherever counts exist, and the payer's printed percentage is kept alongside so
the two can be compared (see validate.py).
"""
from __future__ import annotations
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

FIELDS = [
    "filing_id", "parent_org", "plan_name", "contract_id", "coverage_type",
    "state", "reporting_period",
    "std_total", "std_approved", "std_denied",
    "std_denial_rate", "std_denial_rate_printed",
    "std_appeals_total", "std_appeals_overturned", "std_appeal_overturn_rate",
    "exp_total", "exp_approved", "exp_denied", "exp_denial_rate",
    "ext_review_approved",
    "std_tat_mean_days", "std_tat_median_days",
    "exp_tat_mean_hours", "exp_tat_median_hours",
    "reports_counts", "source_url", "extraction_note", "_segment",
]


def pct(num, den):
    if num is None or den in (None, 0):
        return None
    return round(100.0 * num / den, 2)


def normalize(rec: dict) -> dict:
    out = {k: rec.get(k) for k in FIELDS}

    out["std_denial_rate"] = pct(rec.get("std_denied"), rec.get("std_total"))
    out["std_denial_rate_printed"] = rec.get("std_denied_pct")
    out["exp_denial_rate"] = pct(rec.get("exp_denied"), rec.get("exp_total"))

    overturn = pct(rec.get("std_appeals_overturned"), rec.get("std_appeals_total"))
    out["std_appeal_overturn_rate"] = (
        overturn if overturn is not None else rec.get("std_appeal_overturn_pct")
    )

    # A filing that reports only percentages still gets a usable denial rate.
    if out["std_denial_rate"] is None and rec.get("std_denied_pct") is not None:
        out["std_denial_rate"] = rec["std_denied_pct"]
    if out["exp_denial_rate"] is None and rec.get("exp_denied_pct") is not None:
        out["exp_denial_rate"] = rec["exp_denied_pct"]

    return out


def load(path: Path | None = None) -> list[dict]:
    path = path or (DATA / "merged_filings.json")
    raw = json.loads(path.read_text())
    return [normalize(r) for r in raw]


if __name__ == "__main__":
    rows = load()
    print(f"normalized {len(rows)} filings")
