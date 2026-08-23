"""
Medicare utilization and payment files → normalized price rows.

Three CMS files, each one row per provider (or geography) per service, each
carrying an average *submitted charge* — what the hospital or clinician billed —
beside what Medicare actually allowed and paid. The gap between the two is the
list price the uninsured are billed against, which is why these files matter to
a medical-debt analysis and not only to a Medicare one.

Every ratio here is computed from the two published averages in the same row.
CMS suppresses rows below 11 discharges / beneficiaries before publication, so
a hospital that is absent for a DRG is not a hospital that did none of them.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator, Optional

STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Puerto Rico": "PR", "Virgin Islands": "VI", "Guam": "GU", "American Samoa": "AS",
    "Northern Mariana Islands": "MP",
}


class SchemaError(Exception):
    pass


def _num(v):
    """A published number, or None. Never zero for a blank."""
    if v is None:
        return None
    v = str(v).strip().replace(",", "").replace("$", "")
    if v == "" or v.upper() in ("NA", "N/A", "NULL", "*"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(v):
    f = _num(v)
    return None if f is None else int(round(f))


def _open_csv(path: Path):
    fh = open(path, newline="", encoding="utf-8-sig", errors="replace")
    rdr = csv.DictReader(fh)
    return fh, rdr


def _require(rdr: csv.DictReader, cols: list[str], what: str) -> None:
    have = set(rdr.fieldnames or [])
    missing = [c for c in cols if c not in have]
    if missing:
        raise SchemaError(f"{what}: expected columns not found: {missing}. "
                          f"Header is: {rdr.fieldnames}. CMS renamed something — "
                          f"update pipeline/prices/medicare.py.")


def _ratio(num, den):
    if num is None or den is None or den == 0:
        return None
    return round(num / den, 3)


# ---------------------------------------------------------------------------

def load_inpatient(path: Path, year: str) -> tuple[dict[str, dict], list[dict]]:
    """Returns (hospitals by CCN, inpatient rows)."""
    fh, rdr = _open_csv(path)
    _require(rdr, ["Rndrng_Prvdr_CCN", "Rndrng_Prvdr_Org_Name", "Rndrng_Prvdr_State_Abrvtn",
                   "Rndrng_Prvdr_Zip5", "DRG_Cd", "Tot_Dschrgs", "Avg_Submtd_Cvrd_Chrg",
                   "Avg_Tot_Pymt_Amt", "Avg_Mdcr_Pymt_Amt"], "inpatient")
    hospitals: dict[str, dict] = {}
    rows: list[dict] = []
    with fh:
        for r in rdr:
            ccn = r["Rndrng_Prvdr_CCN"].strip().zfill(6)
            hospitals.setdefault(ccn, _hospital(r, ccn))
            hospitals[ccn]["in_inpatient"] = 1
            charge = _num(r["Avg_Submtd_Cvrd_Chrg"])
            total = _num(r["Avg_Tot_Pymt_Amt"])
            mdcr = _num(r["Avg_Mdcr_Pymt_Amt"])
            rows.append({
                "ccn": ccn,
                "drg": r["DRG_Cd"].strip().zfill(3),
                "drg_desc": r.get("DRG_Desc", "").strip(),
                "discharges": _int(r["Tot_Dschrgs"]),
                "avg_covered_charge": charge,
                "avg_total_payment": total,
                "avg_medicare_payment": mdcr,
                # computed, never generated: list price per dollar actually paid
                "charge_to_payment": _ratio(charge, total),
                "data_year": year,
            })
    return hospitals, rows


def load_outpatient(path: Path, year: str) -> tuple[dict[str, dict], list[dict]]:
    fh, rdr = _open_csv(path)
    _require(rdr, ["Rndrng_Prvdr_CCN", "Rndrng_Prvdr_Org_Name", "Rndrng_Prvdr_State_Abrvtn",
                   "APC_Cd", "Bene_Cnt", "CAPC_Srvcs", "Avg_Tot_Sbmtd_Chrgs",
                   "Avg_Mdcr_Alowd_Amt", "Avg_Mdcr_Pymt_Amt"], "outpatient")
    hospitals: dict[str, dict] = {}
    rows: list[dict] = []
    with fh:
        for r in rdr:
            ccn = r["Rndrng_Prvdr_CCN"].strip().zfill(6)
            hospitals.setdefault(ccn, _hospital(r, ccn))
            hospitals[ccn]["in_outpatient"] = 1
            charge = _num(r["Avg_Tot_Sbmtd_Chrgs"])
            allowed = _num(r["Avg_Mdcr_Alowd_Amt"])
            rows.append({
                "ccn": ccn,
                "apc": r["APC_Cd"].strip(),
                "apc_desc": r.get("APC_Desc", "").strip(),
                "beneficiaries": _int(r["Bene_Cnt"]),
                "services": _int(r["CAPC_Srvcs"]),
                "avg_submitted_charge": charge,
                "avg_allowed": allowed,
                "avg_medicare_payment": _num(r["Avg_Mdcr_Pymt_Amt"]),
                "outlier_services": _int(r.get("Outlier_Srvcs")),
                "avg_outlier_payment": _num(r.get("Avg_Mdcr_Outlier_Amt")),
                "charge_to_allowed": _ratio(charge, allowed),
                "data_year": year,
            })
    return hospitals, rows


def load_physician_geo(path: Path, year: str, codes: Optional[frozenset[str]] = None) -> list[dict]:
    """
    State × HCPCS × place of service — every code in the file, unless a `codes`
    set narrows it. The national rows are kept too (geo = 'US') so a state can
    be read against the country.
    """
    fh, rdr = _open_csv(path)
    _require(rdr, ["Rndrng_Prvdr_Geo_Lvl", "Rndrng_Prvdr_Geo_Cd", "Rndrng_Prvdr_Geo_Desc",
                   "HCPCS_Cd", "Place_Of_Srvc", "Tot_Rndrng_Prvdrs", "Tot_Benes", "Tot_Srvcs",
                   "Avg_Sbmtd_Chrg", "Avg_Mdcr_Alowd_Amt", "Avg_Mdcr_Pymt_Amt",
                   "Avg_Mdcr_Stdzd_Amt"], "physician_geo")
    rows: list[dict] = []
    with fh:
        for r in rdr:
            code = r["HCPCS_Cd"].strip()
            if codes is not None and code not in codes:
                continue
            lvl = r["Rndrng_Prvdr_Geo_Lvl"].strip()
            desc = r["Rndrng_Prvdr_Geo_Desc"].strip()
            if lvl == "National":
                geo = "US"
            else:
                geo = STATE_ABBR.get(desc)
                if geo is None:
                    continue  # "Unknown", "Foreign Country", armed forces etc.
            charge = _num(r["Avg_Sbmtd_Chrg"])
            allowed = _num(r["Avg_Mdcr_Alowd_Amt"])
            rows.append({
                "geo": geo,
                "geo_fips": r["Rndrng_Prvdr_Geo_Cd"].strip() or None,
                "hcpcs": code,
                "hcpcs_desc": r.get("HCPCS_Desc", "").strip(),
                "place_of_service": r["Place_Of_Srvc"].strip(),  # F facility / O office
                "providers": _int(r["Tot_Rndrng_Prvdrs"]),
                "beneficiaries": _int(r["Tot_Benes"]),
                "services": _int(r["Tot_Srvcs"]),
                "avg_submitted_charge": charge,
                "avg_allowed": allowed,
                "avg_medicare_payment": _num(r["Avg_Mdcr_Pymt_Amt"]),
                "avg_standardized": _num(r["Avg_Mdcr_Stdzd_Amt"]),
                "charge_to_allowed": _ratio(charge, allowed),
                "data_year": year,
            })
    return rows


def _hospital(r: dict, ccn: str) -> dict:
    return {
        "ccn": ccn,
        "name": r.get("Rndrng_Prvdr_Org_Name", "").strip(),
        "street": r.get("Rndrng_Prvdr_St", "").strip() or None,
        "city": r.get("Rndrng_Prvdr_City", "").strip() or None,
        "state": r.get("Rndrng_Prvdr_State_Abrvtn", "").strip() or None,
        "state_fips": r.get("Rndrng_Prvdr_State_FIPS", "").strip() or None,
        "zip5": (r.get("Rndrng_Prvdr_Zip5", "").strip() or None),
        "ruca": r.get("Rndrng_Prvdr_RUCA", "").strip() or None,
        "ruca_desc": r.get("Rndrng_Prvdr_RUCA_Desc", "").strip() or None,
        "county_fips": None,      # filled by counties.attach_county
        "in_inpatient": 0,
        "in_outpatient": 0,
        "mrf_status": None,       # filled by mrf
    }


def merge_hospitals(*dicts: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in dicts:
        for ccn, h in d.items():
            if ccn in out:
                out[ccn]["in_inpatient"] |= h["in_inpatient"]
                out[ccn]["in_outpatient"] |= h["in_outpatient"]
                for k, v in h.items():
                    if out[ccn].get(k) in (None, "") and v not in (None, ""):
                        out[ccn][k] = v
            else:
                out[ccn] = dict(h)
    return out
