"""
County context: health outcomes, access, demographics, and medical debt.

Two publishers, one row per county:

  County Health Rankings (UWPHI)   outcomes, access to care, income, race, rurality
  Debt in America (Urban)          share of adults with medical debt in collections,
                                   median amount, split by community composition

Plus the Census ZCTA→county relationship file, which is how a hospital's ZIP
becomes a county so prices can sit beside debt and outcomes.

What is deliberately NOT here: a medical-bankruptcy figure. Bankruptcy filings
are public by court district, but no filing records its cause, and every
"medical bankruptcy" statistic in circulation is a survey estimate. Debt in
collections is the closest thing to a measured public number, so that is what
this dataset carries, under that name.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from . import xlsx

# County Health Rankings variable codes (DataDictionary_2025). Raw value columns
# are <code>_rawvalue. Rates are per the dictionary: v001 years of potential
# life lost per 100,000; v005 per 100,000 Medicare enrollees; v004 and v062 are
# population-per-provider ratios; percentages are 0–1 fractions in the file and
# are stored here as percentages.
CHR_FIELDS: dict[str, tuple[str, str]] = {
    # field name              (code, kind)   kind: 'num' as-is, 'pct' ×100
    "premature_death_ypll":   ("v001", "num"),
    "poor_fair_health_pct":   ("v002", "pct"),
    "uninsured_pct":          ("v085", "pct"),
    "uninsured_adults_pct":   ("v003", "pct"),
    "pcp_ratio":              ("v004", "num"),
    "mhp_ratio":              ("v062", "num"),
    "preventable_stays_rate": ("v005", "num"),
    "median_household_income": ("v063", "num"),
    "income_inequality":      ("v044", "num"),
    "children_in_poverty_pct": ("v024", "pct"),
    "unemployment_pct":       ("v023", "pct"),
    "severe_housing_pct":     ("v136", "pct"),
    "diabetes_pct":           ("v060", "pct"),
    "premature_mortality_aa": ("v127", "num"),
    "population":             ("v051", "num"),
    "black_pct":              ("v054", "pct"),
    "hispanic_pct":           ("v056", "pct"),
    "white_pct":              ("v126", "pct"),
    "rural_pct":              ("v058", "pct"),
}


class SchemaError(Exception):
    pass


def _num(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
    if v == "" or v.upper() in ("NA", "N/A", "NULL", "*", "-", "--"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_chr(path: Path) -> dict[str, dict]:
    """
    The analytic CSV has two header rows: variable codes, then labels. We key on
    the codes. Rows for the nation (fips 00000) and states (county 000) are kept
    with their FIPS so a county can be read against its state.
    """
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rdr = csv.reader(fh)
        first = next(rdr)
        second = next(rdr)
        # The 2025 file puts the human labels on row 1 and the variable codes on
        # row 2; earlier releases were the other way round. Key on whichever row
        # carries the codes rather than on its position.
        if "fipscode" in [c.strip() for c in first]:
            codes, labels = first, second
        else:
            codes, labels = second, first
        idx = {c.strip(): i for i, c in enumerate(codes)}
        need = ["statecode", "countycode", "fipscode", "state", "county", "year"]
        missing = [c for c in need if c not in idx]
        if missing:
            raise SchemaError(f"CHR: identifier columns missing: {missing}; header: {codes[:12]}")
        colmap: dict[str, int] = {}
        absent: list[str] = []
        for field, (code, _) in CHR_FIELDS.items():
            col = f"{code}_rawvalue"
            if col in idx:
                colmap[field] = idx[col]
            else:
                absent.append(col)
        if absent:
            # Not fatal: a measure can be dropped in a release. Leave it NULL and say so.
            print(f"  CHR: {len(absent)} measures absent in this release, left NULL: {absent}")

        out: dict[str, dict] = {}
        for row in rdr:
            if len(row) < len(codes):
                row = row + [""] * (len(codes) - len(row))
            fips = row[idx["fipscode"]].strip().zfill(5)
            rec = {
                "fips": fips,
                "state": row[idx["state"]].strip(),
                "county": row[idx["county"]].strip(),
                "level": "nation" if fips == "00000" else ("state" if fips.endswith("000") else "county"),
                "chr_year": row[idx["year"]].strip(),
            }
            for field, (code, kind) in CHR_FIELDS.items():
                i = colmap.get(field)
                v = _num(row[i]) if i is not None else None
                if v is not None and kind == "pct":
                    v = round(v * 100, 2) if v <= 1.0 else round(v, 2)
                # v004 / v062 are published as providers per resident (0.0014);
                # the column is residents per provider (731), which is how CHR
                # itself reports them on its pages. A value under 1 is the rate.
                if v is not None and field in ("pcp_ratio", "mhp_ratio") and 0 < v < 1:
                    v = round(1 / v, 1)
                rec[field] = v
            out[fips] = rec
        _ = labels
        return out


# ---------------------------------------------------------------------------
# Debt in America — the workbook's header text is matched by meaning, because
# the column order is not promised and the labels have changed between releases.

URBAN_FIELDS: list[tuple[str, str]] = [
    # field, regex against a lower-cased, whitespace-collapsed header
    ("medical_debt_pct",        r"^share .*medical debt in collections.*(all|overall)?$"),
    ("medical_debt_pct_white",  r"share .*medical debt in collections.*(white)"),
    ("medical_debt_pct_color",  r"share .*medical debt in collections.*(color|colour)"),
    ("medical_debt_median",     r"^median .*medical debt in collections.*(all|overall)?$"),
    ("medical_debt_median_white", r"median .*medical debt in collections.*(white)"),
    ("medical_debt_median_color", r"median .*medical debt in collections.*(color|colour)"),
    ("urban_uninsured_pct",     r"share .*without health insurance"),
    ("people_of_color_pct",     r"share of people of color"),
    ("avg_household_income",    r"average household income"),
]
URBAN_FIPS = r"(county fips|fips|county code|geoid)"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _find_header(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows[:15]):
        cells = [_norm(c) for c in row if _norm(c)]
        joined = " | ".join(cells)
        # a header row has several columns; a title row mentioning the same words has one
        if len(cells) >= 3 and "medical debt" in joined:
            return i
    raise SchemaError("Debt in America: could not find a header row mentioning medical debt")


def load_urban(path: Path, level: str = "county") -> tuple[dict[str, dict], dict[str, str]]:
    """
    Returns (records by FIPS, the header→field map actually used), the second so
    the build manifest can show exactly which column fed which field.
    """
    rows = list(xlsx.rows(path))
    h = _find_header(rows)
    header = [_norm(c) for c in rows[h]]
    used: dict[str, str] = {}
    colmap: dict[str, int] = {}
    for field, pat in URBAN_FIELDS:
        rx = re.compile(pat)
        for i, col in enumerate(header):
            if rx.search(col) and i not in colmap.values():
                # the "all" column must not also match the white / color columns
                if field not in ("medical_debt_pct", "medical_debt_median") or not re.search(r"white|color|colour", col):
                    colmap[field] = i
                    used[rows[h][i]] = field
                    break
    fips_i = next((i for i, c in enumerate(header) if re.search(URBAN_FIPS, c)), None)
    if fips_i is None and level == "state":
        fips_i = next((i for i, c in enumerate(header) if c in ("state", "state name", "state abbreviation")), None)
    if fips_i is None and level != "nation":
        raise SchemaError(f"Debt in America: no FIPS/geography column in header {rows[h]}")
    if "medical_debt_pct" not in colmap:
        raise SchemaError(f"Debt in America: no 'share with medical debt in collections' column in {rows[h]}")

    out: dict[str, dict] = {}
    for row in rows[h + 1:]:
        if level == "nation":
            key = "00000"                      # one row; CHR keys the nation as 00000
            if not any(str(c).strip() for c in row):
                continue
        else:
            if fips_i >= len(row):
                continue
            key = str(row[fips_i]).strip()
            if key == "":
                continue
            if re.fullmatch(r"\d+(\.0+)?", key):
                key = str(int(float(key))).zfill(5 if level == "county" else 2)
            if level == "state":
                key = key.zfill(2) + "000"     # CHR keys a state as its FIPS followed by 000
        rec: dict = {"fips": key}
        for field, i in colmap.items():
            v = _num(row[i]) if i < len(row) else None
            if v is not None and "_pct" in field and v <= 1.0:
                v = round(v * 100, 2)
            rec[field] = v
        out[key] = rec
    return out, used


# ---------------------------------------------------------------------------

def load_zcta_county(path: Path) -> dict[str, str]:
    """ZIP5 (as ZCTA) → county FIPS, the county holding most of the ZCTA's land area."""
    best: dict[str, tuple[float, str]] = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rdr = csv.DictReader(fh, delimiter="|")
        need = ["GEOID_ZCTA5_20", "GEOID_COUNTY_20", "AREALAND_PART"]
        missing = [c for c in need if c not in (rdr.fieldnames or [])]
        if missing:
            raise SchemaError(f"ZCTA relationship file: columns missing {missing}; header {rdr.fieldnames}")
        for r in rdr:
            z = r["GEOID_ZCTA5_20"].strip()
            c = r["GEOID_COUNTY_20"].strip()
            if not z or not c:
                continue
            a = _num(r["AREALAND_PART"]) or 0.0
            if z not in best or a > best[z][0]:
                best[z] = (a, c.zfill(5))
    return {z: c for z, (_, c) in best.items()}


def attach_county(hospitals: dict[str, dict], zcta: dict[str, str]) -> int:
    n = 0
    for h in hospitals.values():
        z = (h.get("zip5") or "").strip().zfill(5) if h.get("zip5") else None
        if z and z in zcta:
            h["county_fips"] = zcta[z]
            n += 1
    return n


DEBT_FIELDS = [f for f, _ in URBAN_FIELDS if f.startswith("medical_debt")]
BAN_NOTE = "state bars medical debt from credit reports; the credit-bureau panel cannot see it"
ZERO_NOTE = "reported as exactly zero; treated as not observed, not as no debt"


def build_county_profiles(chr_rows: dict[str, dict], urban: dict[str, dict]) -> list[dict]:
    """
    One row per CHR geography, with Urban's debt columns joined by FIPS.

    A zero is not a zero here. Seven states (CA, CO, IL, NY, RI, VT, WA in the
    2025 release) bar medical debt from consumer credit reports, so the panel
    records 0% for every county in them. Publishing that as 0% would say those
    states have no medical debt; it says nothing of the kind. The state-level
    row is the tell: a state at exactly 0 is a reporting ban, and every county
    in it gets NULL debt columns and a note. An isolated county at exactly 0
    elsewhere is treated the same way, with a different note.
    """
    banned = {fips[:2] for fips, u in urban.items()
              if len(fips) == 5 and fips.endswith("000") and fips != "00000"
              and u.get("medical_debt_pct") == 0.0}
    out = []
    for fips, rec in chr_rows.items():
        row = dict(rec)
        u = urban.get(fips, {})
        for field, _ in URBAN_FIELDS:
            row[field] = u.get(field)
        row["medical_debt_note"] = None
        if fips != "00000" and fips[:2] in banned:
            for f in DEBT_FIELDS:
                row[f] = None
            row["medical_debt_note"] = BAN_NOTE
        elif row.get("medical_debt_pct") == 0.0:
            for f in DEBT_FIELDS:
                row[f] = None
            row["medical_debt_note"] = ZERO_NOTE
        out.append(row)
    return out
