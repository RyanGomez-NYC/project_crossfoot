"""
Ingest CMS's MCPAR public-use file -- the government's own copy of the data.

States file a Managed Care Program Annual Report for every Medicaid managed
care program (42 CFR 438.66), and starting with reports submitted in June 2026
that filing carries plan-level prior authorization metrics: request volumes,
approval and denial percentages, approved-after-appeal, extended-timeframe
approvals, and mean/median decision times. CMS publishes the collected reports
as one CSV on data.medicaid.gov.

That file is the eventual mass source for the Medicaid side of this dataset:
one download instead of hundreds of payer websites. The catch is timing -- each
PUF holds the reporting periods states filed that cycle, and the release
carrying CY2025 periods lags the filings. This module downloads whatever the
current release is, keeps only plans whose reporting period covers most of
2025, and writes them as filings; run it against each new release until the
CY2025 wave appears.

    python3 -m pipeline.preauth.mcpar            # use the cached download
    python3 -m pipeline.preauth.mcpar --refresh  # re-download first

Dataset: data.medicaid.gov, search "MCPAR" (2024 PUF id 66da70e7-228e-41aa-
b041-6f9e433ff237). Percentages arrive without counts for denials; counts for
totals arrive whole -- both are kept exactly as filed, and approved/denied
counts are left NULL rather than derived, per the project's never-compute rule.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

from .docs import UA  # one identity for everything this project fetches

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "preauth" / "mcpar_puf_2024.csv"
OUT = ROOT / "data" / "seg_w5_mcpar.json"
URL = "https://download.medicaid.gov/data/mmcc-mcpar-puf-2024.csv"

Q = "PriorAuthorization"


def _d(s: str) -> date | None:
    m = re.match(r"(\d+)/(\d+)/(\d+)", s or "")
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def _num(v: str):
    v = (v or "").replace(",", "").strip()
    if v.lower() in ("", "nr", "n/a", "na"):
        return None
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return None


def covers_2025(start: date | None, end: date | None) -> bool:
    """At least nine months of the period fall inside CY2025."""
    if not start or not end:
        return False
    lo = max(start, date(2025, 1, 1))
    hi = min(end, date(2025, 12, 31))
    return (hi - lo).days >= 270


def load(path: Path = RAW) -> list[dict]:
    byplan: dict[tuple, dict] = defaultdict(dict)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if Q not in r["Question_ID"] or not r["Question_ID"].startswith("plan_"):
                continue
            key = (r["State"], r["Plan_or_BSS"],
                   r["Reporting_Period_Start_Date"], r["Reporting_Period_End_Date"])
            byplan[key][r["Question_ID"].replace("plan_", "")] = r["Response"]

    rows = []
    for (state, plan, ps, pe), v in byplan.items():
        if not covers_2025(_d(ps), _d(pe)):
            continue
        std_total = _num(v.get("totalStandardPriorAuthorizationRequestsReceived"))
        rows.append({
            "parent_org": plan, "plan_name": plan, "contract_id": None,
            "coverage_type": "Medicaid Managed Care", "state": state[:2].upper()
            if len(state) == 2 else None,
            "_state_name": state,
            "reporting_period": f"{ps} - {pe}",
            "std_total": std_total,
            "std_approved": None, "std_denied": None,
            "std_denied_pct": _num(v.get("percentageOfStandardPriorAuthorizationRequestsDenied")),
            "std_appeals_total": None,
            "std_appeals_overturned": None,
            "ext_review_approved": None,
            "exp_total": _num(v.get("totalExpeditedPriorAuthorizationRequestsReceived")),
            "exp_approved": None, "exp_denied": None,
            "exp_denied_pct": _num(v.get("percentageOfExpeditedPriorAuthorizationRequestsDenied")),
            "std_tat_mean_days": _num(v.get("averageTimeToDecisionForStandardPriorAuthorizations")),
            "std_tat_median_days": _num(v.get("medianTimeToDecisionOnStandardPriorAuthorizations")),
            "exp_tat_mean_hours": _num(v.get("averageTimeToDecisionForExpeditedPriorAuthorizations")),
            "exp_tat_median_hours": _num(v.get("medianTimeToDecisionOnExpeditedPriorAuthorizationRequests")),
            "reports_counts": std_total is not None,
            "service_list_url": (v.get("urlForListOfAllItemsAndServicesSubjectToPriorAuthorization")
                                 or "").strip() or None,
            "source_url": URL,
            "source_sha256": None,
            "extraction_note": "From CMS's MCPAR public-use file -- the state's own filing "
                               "to CMS under 42 CFR 438.66, not the plan's website posting. "
                               "MCPAR reports percentages and totals; approved/denied counts "
                               "are not filed and stay null. Expedited decision times are "
                               "filed in hours per the MCPAR template.",
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    if args.refresh or not RAW.exists():
        print("downloading MCPAR PUF...")
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        RAW.write_bytes(urllib.request.urlopen(req, timeout=600).read())

    rows = load()
    # Show what the file holds either way, so a run against a pre-CY2025
    # release says clearly why it wrote nothing.
    all_periods = set()
    with open(RAW, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if Q in r["Question_ID"] and r["Question_ID"].startswith("plan_total"):
                all_periods.add((r["Reporting_Period_Start_Date"],
                                 r["Reporting_Period_End_Date"]))
    print(f"reporting periods with PA volume data in this release: {sorted(all_periods)}")
    print(f"{len(rows)} plans with periods covering CY2025")
    if rows:
        OUT.write_text(json.dumps({"filings": rows}, indent=1) + "\n")
        print(f"-> {OUT.relative_to(ROOT)}  (register in pipeline/merge.py SEGMENTS)")
    else:
        print("nothing to write yet -- re-run against the next PUF release "
              "(data.medicaid.gov, search MCPAR)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
