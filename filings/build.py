"""Build the published artifacts: CSV, XLSX, and the validation report."""
from __future__ import annotations
import csv
from pathlib import Path

from normalize import load, FIELDS
from validate import run as run_validation

OUT = Path(__file__).resolve().parents[1] / "out"
OUT.mkdir(exist_ok=True)

EXPORT_COLS = [
    "filing_id", "parent_org", "plan_name", "contract_id", "coverage_type",
    "state", "reporting_period",
    "std_total", "std_approved", "std_denied", "std_denial_rate",
    "std_appeals_total", "std_appeals_overturned", "std_appeal_overturn_rate",
    "exp_total", "exp_approved", "exp_denied", "exp_denial_rate",
    "std_tat_mean_days", "std_tat_median_days",
    "exp_tat_mean_hours", "exp_tat_median_hours",
    "reports_counts", "quality_flags", "source_url", "extraction_note",
]


def build():
    rows = load()
    findings = run_validation(rows)

    by_filing: dict[str, list[str]] = {}
    for f in findings:
        by_filing.setdefault(f["filing_id"], []).append(f"{f['severity']}:{f['rule']}")

    for r in rows:
        r["quality_flags"] = "; ".join(by_filing.get(r["filing_id"], [])) or "clean"

    with (OUT / "pa_metrics_2025.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=EXPORT_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    with (OUT / "validation_findings.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["filing_id", "rule", "severity", "detail"])
        w.writeheader()
        w.writerows(findings)

    return rows, findings


if __name__ == "__main__":
    rows, findings = build()
    errs = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warn")
    clean = sum(1 for r in rows if r["quality_flags"] == "clean")
    print(f"{len(rows)} filings -> out/pa_metrics_2025.csv")
    print(f"{len(findings)} findings ({errs} error, {warns} warn) -> out/validation_findings.csv")
    print(f"{clean} of {len(rows)} filings passed every check")
