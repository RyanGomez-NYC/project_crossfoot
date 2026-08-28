"""
Build the CMS-0057-F reporting universe -- the denominator.

Every payer the rule obliges to publish CY2025 prior authorization metrics gets
one row, keyed at the level the rule makes it report:

    Medicare Advantage organizations      per contract      ma:H1234
    Medicaid managed care plans           per plan          mcd:TX:plan-slug
    CHIP managed care entities            per plan          chip:TX:plan-slug
    QHP issuers on the FFEs               per issuer        qhp:TX:12345
    State Medicaid / CHIP FFS programs    per state         ffs:TX / chipffs:TX

Why this file is the point of the whole exercise: without a denominator,
"448 filings collected" is a number with no population behind it. With one, the
same collection becomes a compliance measurement -- which payers published, which
did not, by segment and by state -- and that is a finding no amount of extra
scraping produces on its own.

It also carries enrollment, which the rule does not require payers to publish and
most do not. For MA and Medicaid it is free here, straight from the CMS files.

    python3 -m pipeline.preauth.universe            # writes data/seeds/
    python3 -m pipeline.preauth.universe --refresh  # re-download the CMS files first

Outputs:
    data/seeds/pa_entities.csv          one row per obliged reporter
    data/seeds/pa_ma_contract_state.csv contract x state x enrollment

The second file is the contract-to-state map. It exists so a national MA filing
can be attributed to the states its contract actually serves WITHOUT allocating
counts across them. Attribution is a join; allocation would be invention.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

from .docs import UA  # one identity for everything this project fetches
from .sources import SOURCES, MA_ORG_TYPES

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "preauth"
SEEDS = ROOT / "data" / "seeds"

MEDICAID_MC_URL = "https://download.medicaid.gov/data/managed-care-enrollment-by-program-and-plan2024-table4.csv"
QHP_ATTR_URL = "https://download.cms.gov/marketplace-puf/2026/plan-attributes-puf.zip"

STATE_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Puerto Rico": "PR", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
# The 52 FFS programs. Territories other than PR run CHIP/Medicaid without
# managed care reporting obligations under this rule; PR is included because it
# has both and authdenied counts it.
FFS_STATES = sorted(set(STATE_CODE.values()))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def fetch(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=300) as r:
        dest.write_bytes(r.read())
    return dest


def unzip(zpath: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    return dest


def refresh() -> None:
    """Re-download every source. Slow (the CPSC zip is ~36 MB); rarely needed."""
    for s in SOURCES.values():
        print(f"  {s.key} ...", flush=True)
        z = fetch(s.url, RAW / f"{s.key}.zip")
        unzip(z, RAW / s.local)
    fetch(MEDICAID_MC_URL, RAW / "medicaid_mc_plans_2024.csv")
    unzip(fetch(QHP_ATTR_URL, RAW / "qhp-plan-attributes-2026.zip"), RAW / "qhp_plan_attributes")


def find(pattern: str) -> Path:
    hits = sorted(RAW.rglob(pattern))
    if not hits:
        sys.exit(f"missing source file: {pattern} under {RAW}\n"
                 f"run: python3 -m pipeline.preauth.universe --refresh")
    return hits[-1]          # newest vintage sorts last


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def as_int(v: str) -> int | None:
    v = (v or "").replace(",", "").strip()
    return int(v) if v.isdigit() else None


# --------------------------------------------------------------------------
# Medicare Advantage: one row per contract.
#
# The contract directory carries 921 contracts; only the four organization types
# in MA_ORG_TYPES are Medicare Advantage organizations. PACE organizations,
# 1876 Cost plans, HCPPs and the LI NET sponsor are not, and owe nothing here.
#
# Two further filters, both about CY2025 rather than about the payer:
#   - a contract effective in 2026 did not operate in 2025 and has nothing to
#     report for it;
#   - a contract with no enrollment has no requests to report.
# Both are recorded on the row, so a later run can widen the universe without
# re-deriving why a contract was left out.
# --------------------------------------------------------------------------
def build_ma() -> tuple[list[dict], list[dict]]:
    direct = read_csv(find("MA_Contract_directory_*.csv"))
    sa = read_csv(find("MA_Cnty_SA_*.csv"))
    enr = read_csv(find("CPSC_Enrollment_Info_*.csv"))

    # contract -> {state: enrollment}. '*' is CMS suppression of a cell under 11
    # enrollees: censored, not zero, so it contributes to the state list but not
    # to the sum.
    by_state: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in enr:
        st = (r.get("State") or "").strip()
        if not st:
            continue
        n = as_int(r.get("Enrollment", ""))
        by_state[r["Contract Number"]][st] += n or 0

    # The service area is the legal answer to "which states does this contract
    # cover"; enrollment is the practical one. A contract can be approved in a
    # state and have nobody in it. Keep both.
    area: dict[str, set[str]] = defaultdict(set)
    for r in sa:
        st = (r.get("State") or "").strip()
        if st:
            area[r["Contract ID"]].add(st)

    entities, cs_rows = [], []
    for r in direct:
        cid = r["Contract Number"].strip()
        if r["Organization Type"] not in MA_ORG_TYPES:
            continue
        eff = r.get("Contract Effective Date", "")
        eff_year = int(eff[-4:]) if re.match(r"\d\d/\d\d/\d{4}", eff or "") else None
        total = as_int(r.get("Enrollment", ""))
        in_scope = (eff_year or 0) < 2026 and (total or 0) > 0
        states = sorted(area.get(cid) or by_state.get(cid, {}).keys())
        entities.append({
            "entity_id": f"ma:{cid}",
            "segment": "Medicare Advantage",
            "filing_unit": "ma_contract",
            "entity_key": cid,
            "entity_name": r["Organization Marketing Name"].strip(),
            "legal_entity": r["Legal Entity Name"].strip(),
            "parent_org": r["Parent Organization"].strip(),
            "state": states[0] if len(states) == 1 else "",
            "states_served": "|".join(states),
            "n_states": len(states),
            "enrollment": total if total is not None else "",
            "enrollment_source": "CMS MA contract directory 2026-08",
            "plan_type": r["Plan Type"].strip(),
            "contract_effective": eff,
            "in_scope_cy2025": "yes" if in_scope else "no",
            "out_of_scope_reason": "" if in_scope else (
                "contract effective after CY2025" if (eff_year or 0) >= 2026
                else "no enrollment reported"),
            "expected_domain": "",
            "notes": "",
        })
        for st, n in sorted(by_state.get(cid, {}).items()):
            cs_rows.append({"contract_id": cid, "state": st, "enrollment": n,
                            "in_service_area": "yes" if st in area.get(cid, ()) else "no"})
    return entities, cs_rows


# --------------------------------------------------------------------------
# Medicaid managed care: one row per plan per state.
#
# The CMS enrollment table lists every managed care arrangement, including PACE,
# NEMT brokers and PCCM entities. Only comprehensive MCOs (with or without
# MLTSS) are kept as the primary universe: 416 distinct state+plan pairs, which
# is the same population authdenied reports as its 425 "Medicaid MCO" segment.
# The limited-benefit plans (BHO, MLTSS-only, dental, NEMT) are PIHPs and PAHPs
# and are arguably in scope too; they are emitted with tier=secondary so the
# headline denominator stays comparable while nothing is thrown away.
# --------------------------------------------------------------------------
def _mc_type(program: str) -> str:
    m = re.search(r"\(([^)]*)\)\s*$", program or "")
    return m.group(1) if m else ""


def build_medicaid() -> list[dict]:
    rows = read_csv(find("medicaid_mc_plans_2024.csv"))
    year = max(r["Year"] for r in rows)
    rows = [r for r in rows if r["Year"] == year]

    best: dict[tuple, dict] = {}
    for r in rows:
        kind = _mc_type(r["Program Name"])
        if "PACE" in kind or "PCCM" in kind:
            continue
        state = STATE_CODE.get(r["State"].strip())
        plan = (r["Plan Name"] or "").strip()
        # California's table lists one row per plan PER COUNTY, with the county
        # appended after a slash ("Kaiser Permanente/ Marin"). The rule's
        # reporting unit is the plan, and Kaiser publishes two Medi-Cal reports,
        # not thirty. Collapsing on the name before the slash makes the
        # denominator count obligations rather than service areas.
        plan = plan.split("/")[0].strip()
        # Corporate dressing is not identity: "Health Net Community Solutions"
        # and "Health Net Community Solutions, Inc." are one plan, and leaving
        # both in the table makes every match against either ambiguous. Strip
        # the suffixes before the slug is cut.
        plan = re.sub(r"[,.]?\s+(inc|llc|l\.l\.c|lp|corp|corporation|company|co)\.?$",
                      "", plan, flags=re.I).strip()
        if not state or not plan:
            continue
        primary = "Comprehensive MCO" in kind
        key = (state, slug(plan))
        n = as_int(r.get("Total Enrollment", ""))
        prev = best.get(key)
        if prev is not None:
            # The same plan again -- another county or another program. Its
            # enrollment adds; its identity does not.
            prev["_n"] = (prev["_n"] or 0) + (n or 0)
            prev["enrollment"] = prev["_n"]
            if primary:
                prev["_primary"] = True
            continue
        if prev is None or (n or 0) > (prev["_n"] or 0):
            best[key] = {
                "entity_id": f"mcd:{state}:{slug(plan)}",
                "segment": "Medicaid Managed Care",
                "filing_unit": "medicaid_plan",
                "entity_key": slug(plan),
                "entity_name": plan,
                "legal_entity": "",
                "parent_org": (r.get("Parent Organization") or "").strip(),
                "state": state,
                "states_served": state,
                "n_states": 1,
                "enrollment": n if n is not None else "",
                "enrollment_source": f"CMS Medicaid managed care enrollment {year}",
                "plan_type": kind,
                "contract_effective": "",
                "in_scope_cy2025": "yes",
                "out_of_scope_reason": "",
                "expected_domain": "",
                "notes": f"program: {r['Program Name']}",
                "_n": n, "_primary": primary,
            }
        elif primary:
            best[key]["_primary"] = True
    out = []
    for v in best.values():
        v["tier"] = "primary" if v.pop("_primary") else "secondary"
        v.pop("_n")
        out.append(v)
    return out


# --------------------------------------------------------------------------
# QHP issuers on the federally facilitated exchanges: one row per issuer.
#
# The rule reaches QHP issuers on the FFEs only -- not state-based exchanges --
# so the plan-attributes PUF, which covers exactly the healthcare.gov states, is
# the right frame. Dental-only plans are excluded by the rule; an issuer that
# sells nothing but dental is not in scope.
# --------------------------------------------------------------------------
def build_qhp() -> list[dict]:
    path = find("plan-attributes-puf.csv")
    seen: dict[tuple, dict] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("DentalOnlyPlan") or "").strip().lower() == "yes":
                continue
            st, iid = r["StateCode"].strip(), r["IssuerId"].strip()
            if not st or not iid:
                continue
            key = (st, iid)
            if key in seen:
                continue
            seen[key] = {
                "entity_id": f"qhp:{st}:{iid}",
                "segment": "Marketplace QHP",
                "filing_unit": "qhp_issuer",
                "entity_key": iid,
                "entity_name": (r.get("IssuerMarketPlaceMarketingName") or "").strip(),
                "legal_entity": "",
                "parent_org": "",
                "state": st,
                "states_served": st,
                "n_states": 1,
                "enrollment": "",
                "enrollment_source": "",
                "plan_type": (r.get("MarketCoverage") or "").strip(),
                "contract_effective": "",
                "in_scope_cy2025": "yes",
                "out_of_scope_reason": "",
                "expected_domain": "",
                "notes": "FFE issuer (healthcare.gov state)",
                "tier": "primary",
            }
    return list(seen.values())


# --------------------------------------------------------------------------
# Fee-for-service: one row per state Medicaid programme and one per state CHIP
# programme. There is no file to read -- the obligation follows from the state
# running the programme at all.
# --------------------------------------------------------------------------
def build_ffs() -> list[dict]:
    out = []
    for st in FFS_STATES:
        for pref, seg, unit in (("ffs", "Medicaid FFS", "ffs_state"),
                                ("chipffs", "CHIP FFS", "ffs_state")):
            out.append({
                "entity_id": f"{pref}:{st}",
                "segment": seg,
                "filing_unit": unit,
                "entity_key": st,
                "entity_name": f"{st} {'Medicaid' if pref == 'ffs' else 'CHIP'} (fee-for-service)",
                "legal_entity": "", "parent_org": "",
                "state": st, "states_served": st, "n_states": 1,
                "enrollment": "", "enrollment_source": "",
                "plan_type": "fee-for-service", "contract_effective": "",
                "in_scope_cy2025": "yes", "out_of_scope_reason": "",
                "expected_domain": "", "notes": "state agency obligation",
                "tier": "primary",
            })
    return out


FIELDS = ["entity_id", "segment", "filing_unit", "entity_key", "entity_name",
          "legal_entity", "parent_org", "state", "states_served", "n_states",
          "enrollment", "enrollment_source", "plan_type", "contract_effective",
          "in_scope_cy2025", "out_of_scope_reason", "tier", "expected_domain", "notes"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download the CMS source files")
    args = ap.parse_args()
    if args.refresh:
        print("refreshing sources")
        refresh()

    SEEDS.mkdir(parents=True, exist_ok=True)
    ma, cs = build_ma()
    for r in ma:
        r["tier"] = "primary"
    rows = ma + build_medicaid() + build_qhp() + build_ffs()

    with open(SEEDS / "pa_entities.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(SEEDS / "pa_ma_contract_state.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["contract_id", "state", "enrollment", "in_service_area"])
        w.writeheader()
        w.writerows(cs)

    inscope = [r for r in rows if r["in_scope_cy2025"] == "yes" and r["tier"] == "primary"]
    print(f"\npa_entities.csv: {len(rows)} rows, {len(inscope)} in-scope primary")
    seg = defaultdict(int)
    for r in inscope:
        seg[r["segment"]] += 1
    for k in sorted(seg, key=lambda k: -seg[k]):
        print(f"  {seg[k]:5d}  {k}")
    print(f"pa_ma_contract_state.csv: {len(cs)} contract-state pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
