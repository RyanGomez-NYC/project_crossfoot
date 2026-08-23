"""
Enrichment: disease rates, age and poverty, and health systems.

  CDC PLACES         model-based prevalence per county — obesity, diabetes, COPD,
                     heart disease, stroke, depression, hypertension, no insurance,
                     no routine checkup. Adds the morbidity side of the picture.
  ACS 5-year 2023    median age, share aged 65+, share below poverty. The models
                     need age as a control: it explains much of morbidity by itself.
  AHRQ Compendium    hospital → health system → corporate parent. Makes "which
                     systems charge the most" a question the data can answer.

Standard library only. Every loader tolerates a renamed column by naming what it
could not find, so a new release fails loudly rather than silently blank.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

# PLACES measure id -> (field, label). Crude prevalence is what a county's
# residents experience; age-adjusted is what the models use when age is not a
# feature. Both are kept; the site shows crude and says so.
PLACES_MEASURES = {
    "OBESITY":    ("obesity_pct",      "Obesity"),
    "DIABETES":   ("diabetes_places_pct", "Diagnosed diabetes"),
    "COPD":       ("copd_pct",         "COPD"),
    "CHD":        ("chd_pct",          "Coronary heart disease"),
    "STROKE":     ("stroke_pct",       "Stroke"),
    "BPHIGH":     ("bphigh_pct",       "High blood pressure"),
    "CKD":        ("ckd_pct",          "Chronic kidney disease"),
    "DEPRESSION": ("depression_pct",   "Depression"),
    "CASTHMA":    ("asthma_pct",       "Current asthma"),
    "CSMOKING":   ("smoking_pct",      "Current smoking"),
    "ACCESS2":    ("no_insurance_18_64_pct", "No health insurance, 18–64"),
    "CHECKUP":    ("checkup_pct",      "Routine checkup in past year"),
    "GHLTH":      ("fair_poor_health_places_pct", "Fair or poor health"),
    "MHLTH":      ("poor_mental_health_pct", "Frequent mental distress"),
    "PHLTH":      ("poor_physical_health_pct", "Frequent physical distress"),
    "DISABILITY": ("disability_pct",   "Any disability"),
    "FOODINSECU": ("food_insecurity_pct", "Food insecurity"),
}


class SchemaError(Exception):
    pass


def _num(v):
    if v is None:
        return None
    v = str(v).strip()
    if v == "" or v.upper() in ("NA", "N/A", "NULL", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_places(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (crude by FIPS, age-adjusted by FIPS)."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rdr = csv.DictReader(fh)
        cols = set(rdr.fieldnames or [])
        if "CountyFIPS" not in cols:
            raise SchemaError(f"PLACES: no CountyFIPS column; header starts {list(cols)[:8]}")
        missing = [m for m in PLACES_MEASURES if f"{m}_CrudePrev" not in cols]
        if missing:
            print(f"  PLACES: measures absent in this release, left NULL: {missing}")
        crude: dict[str, dict] = {}
        adj: dict[str, dict] = {}
        for r in rdr:
            fips = (r.get("CountyFIPS") or "").strip().zfill(5)
            if not fips or fips == "00000":
                continue
            c, a = {}, {}
            for m, (field, _) in PLACES_MEASURES.items():
                c[field] = _num(r.get(f"{m}_CrudePrev"))
                a[field] = _num(r.get(f"{m}_AdjPrev"))
            c["places_population"] = _num(r.get("TotalPopulation"))
            crude[fips] = c
            adj[fips] = a
        return crude, adj


def load_acs(path: Path) -> dict[str, dict]:
    """The Census API returns a JSON array of arrays with a header row."""
    text = path.read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # An HTML "Missing Key" page means the API wants a key. Do not leave
        # the bad payload on disk or every later run silently reuses it.
        hint = ("the API answered with an HTML page titled 'Missing Key' — it now requires "
                "an API key. Register one (free) at https://api.census.gov/data/key_signup.html, "
                "append &key=... to the acs URL in pipeline/prices/sources.py, delete the raw "
                "file, and re-run --central."
                if "<html" in text[:200].lower() else "response is not JSON")
        path.unlink(missing_ok=True)
        (path.parent / (path.name + ".SOURCE.json")).unlink(missing_ok=True)
        raise SchemaError(f"ACS: {hint}") from None
    if not data or not isinstance(data[0], list):
        raise SchemaError("ACS: response is not the expected array-of-arrays")
    hdr = data[0]
    idx = {c: i for i, c in enumerate(hdr)}
    need = ["B01002_001E", "B17001_001E", "B17001_002E", "B01001_001E", "state", "county"]
    missing = [c for c in need if c not in idx]
    if missing:
        raise SchemaError(f"ACS: columns missing {missing}; header {hdr}")
    m65 = [f"B01001_0{n}E" for n in range(20, 26)]
    f65 = [f"B01001_0{n}E" for n in range(44, 50)]
    out: dict[str, dict] = {}
    for row in data[1:]:
        fips = row[idx["state"]].zfill(2) + row[idx["county"]].zfill(3)
        pop = _num(row[idx["B01001_001E"]])
        pov_den = _num(row[idx["B17001_001E"]])
        pov_num = _num(row[idx["B17001_002E"]])
        over65 = sum((_num(row[idx[c]]) or 0.0) for c in m65 + f65 if c in idx)
        out[fips] = {
            "median_age": _num(row[idx["B01002_001E"]]),
            "poverty_pct": round(100 * pov_num / pov_den, 2) if pov_num is not None and pov_den else None,
            "age65_pct": round(100 * over65 / pop, 2) if pop else None,
        }
    return out


# AHRQ column names have drifted between releases; match by meaning.
_AHRQ_ALIASES = {
    "ccn":            ["ccn", "medicare_provider_number", "mcrnum", "provider_number", "prvdr_num"],
    "health_sys_id":  ["health_sys_id", "hsid", "system_id"],
    "health_sys_name": ["health_sys_name", "health_system_name", "system_name", "hsname"],
    "health_sys_state": ["health_sys_state", "hs_state", "system_state"],
    "corp_parent_id": ["corp_parent_id", "corporate_parent_id"],
    "corp_parent_name": ["corp_parent_name", "corporate_parent_name"],
    "hospital_name":  ["hospital_name", "hosp_name", "name"],
    "beds":           ["hos_beds", "hospital_beds", "beds", "total_beds"],
    "ownership":      ["hos_ownership", "ownership", "hospital_ownership"],
}


def load_ahrq(path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """Returns (by CCN, the header→field map used)."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rdr = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in (rdr.fieldnames or [])}
        colmap: dict[str, str] = {}
        for field, names in _AHRQ_ALIASES.items():
            for n in names:
                if n in cols:
                    colmap[field] = cols[n]
                    break
        for req in ("ccn", "health_sys_id", "health_sys_name"):
            if req not in colmap:
                raise SchemaError(f"AHRQ linkage: no column for {req}; header {rdr.fieldnames}")
        out: dict[str, dict] = {}
        for r in rdr:
            ccn = (r.get(colmap["ccn"]) or "").strip()
            if not ccn:
                continue
            ccn = ccn.zfill(6)
            sid = (r.get(colmap["health_sys_id"]) or "").strip()
            if not sid:
                continue  # independent hospital: no system
            out[ccn] = {
                "system_id": sid,
                "system_name": (r.get(colmap["health_sys_name"]) or "").strip(),
                "system_state": (r.get(colmap.get("health_sys_state", ""), "") or "").strip() or None,
                "corp_parent_id": (r.get(colmap.get("corp_parent_id", ""), "") or "").strip() or None,
                "corp_parent_name": (r.get(colmap.get("corp_parent_name", ""), "") or "").strip() or None,
                "beds": _num(r.get(colmap.get("beds", ""), "")),
                "ownership": (r.get(colmap.get("ownership", ""), "") or "").strip() or None,
            }
        return out, {v: k for k, v in colmap.items()}


def attach_systems(hospitals: dict[str, dict], ahrq: dict[str, dict]) -> int:
    n = 0
    for ccn, h in hospitals.items():
        a = ahrq.get(ccn)
        h["system_id"] = a["system_id"] if a else None
        h["system_name"] = a["system_name"] if a else None
        h["beds"] = a["beds"] if a else None
        h["ownership"] = a["ownership"] if a else None
        if a:
            n += 1
    return n


def build_systems(hospitals: dict[str, dict], inpatient: list[dict], ahrq: dict[str, dict]) -> list[dict]:
    """
    One row per health system present in the Medicare files: member hospitals,
    states, discharges, and the discharge-weighted billed-per-dollar-paid across
    every member's every DRG. Computed, never copied.
    """
    acc: dict[str, dict] = defaultdict(lambda: {"hospitals": set(), "states": set(), "c": 0.0, "p": 0.0, "dis": 0})
    meta: dict[str, dict] = {}
    for r in inpatient:
        h = hospitals.get(r["ccn"])
        if not h or not h.get("system_id"):
            continue
        sid = h["system_id"]
        a = acc[sid]
        a["hospitals"].add(r["ccn"])
        if h.get("state"):
            a["states"].add(h["state"])
        d = r.get("discharges") or 0
        if r.get("avg_covered_charge") is not None and r.get("avg_total_payment") is not None and d:
            a["c"] += r["avg_covered_charge"] * d
            a["p"] += r["avg_total_payment"] * d
            a["dis"] += d
        if sid not in meta:
            src = ahrq.get(r["ccn"], {})
            meta[sid] = {"name": h["system_name"], "state": src.get("system_state"),
                         "corp_parent_id": src.get("corp_parent_id"), "corp_parent_name": src.get("corp_parent_name")}
    out = []
    for sid, a in acc.items():
        m = meta.get(sid, {})
        out.append({
            "system_id": sid,
            "name": m.get("name"),
            "state": m.get("state"),
            "corp_parent_id": m.get("corp_parent_id"),
            "corp_parent_name": m.get("corp_parent_name"),
            "hospitals_n": len(a["hospitals"]),
            "states_n": len(a["states"]),
            "states": ",".join(sorted(a["states"])),
            "discharges": a["dis"],
            "billed": round(a["c"], 2),
            "paid": round(a["p"], 2),
            "charge_to_payment": round(a["c"] / a["p"], 3) if a["p"] else None,
        })
    out.sort(key=lambda r: -(r["charge_to_payment"] or 0))
    return out


def attach_county_enrichment(county_rows: list[dict], places_crude: dict, places_adj: dict, acs: dict) -> None:
    for c in county_rows:
        pc = places_crude.get(c["fips"], {})
        pa = places_adj.get(c["fips"], {})
        for field, _ in PLACES_MEASURES.values():
            c[field] = pc.get(field)
            c[field + "_adj"] = pa.get(field)
        a = acs.get(c["fips"], {})
        c["median_age"] = a.get("median_age")
        c["poverty_pct"] = a.get("poverty_pct")
        c["age65_pct"] = a.get("age65_pct")
