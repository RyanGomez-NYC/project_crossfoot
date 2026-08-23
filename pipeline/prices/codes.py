"""
The code dictionary: one row per billing code the analysis has seen, with a
status that says whether it is a code at all, and a description that says
where its definition came from.

Why this exists. The hospital price files are parsed for every CPT/HCPCS and
MS-DRG string they carry — 78,939 distinct codes in the first crawl. The CPT
book has about 10,000 codes and the HCPCS Level II list about 7,000, so most
of those strings are chargemaster item numbers, revenue codes and ranges that
happen to look like codes. A definition cannot exist for a number that is not
a code, and the site must not present one as if it were.

Status, in descending order of trust:
    official       in a public CMS list: the physician file (codes Medicare
                   paid for), the inpatient file (MS-DRGs with discharges), the
                   HCPCS Level II release, or the MS-DRG definitions table
    hospital_only  well-formed, not in any CMS list, and listed by at least two
                   sampled hospitals — a real code Medicare does not pay for,
                   or a common local convention; we cannot tell which
    unverified     malformed, or well-formed but seen in one hospital's file
                   only — nothing corroborates it

Description source, in the same order:
    cms            the CMS file's own descriptor (HCPCS_Desc / DRG_Desc)
    hcpcs          the HCPCS Level II release's long description
    msdrg          the MS-DRG definitions table title
    hospital       the most common description across the sampled hospitals'
                   files, with desc_n = how many files used that wording
    basket         basket.py's own plain-language label (basket items only,
                   when no source above carries text)
    (none)         no public descriptor exists. CPT Category I descriptions are
                   AMA copyright; for a CPT code Medicare did not pay for there
                   is no text we may print, and the row says so.

Nothing is invented. A status is a set-membership test; a description is a
string copied from a named source.
"""
from __future__ import annotations

import csv
import io
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional

from . import xlsx

# CPT Category I (5 digits), Category II (4 digits + F), Category III (+T),
# PLA (+U), MAAA (+M); HCPCS Level II (letter A–V + 4 digits).
CPT_RE = re.compile(r"^(?:\d{5}|\d{4}[FTUM]|[A-V]\d{4})$")
DRG_RE = re.compile(r"^\d{3}$")

STATUS_OFFICIAL, STATUS_HOSPITAL, STATUS_UNVERIFIED = "official", "hospital_only", "unverified"


def well_formed(ctype: str, code: str) -> bool:
    if ctype == "CPT":
        return bool(CPT_RE.match(code))
    if ctype == "MS-DRG":
        return bool(DRG_RE.match(code)) and code != "000"
    return False


# ---------------------------------------------------------------------------
# official lists

def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def load_hcpcs_release(path: Path) -> dict[str, str]:
    """
    The CMS quarterly HCPCS Level II release: a zip holding an .xlsx (and/or a
    fixed-width .txt). Returns {code: long description}. Columns are found by
    name, so a renamed sheet or a new column order does not break it.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    members: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith((".xlsx", ".csv", ".txt")) and not n.startswith("__MACOSX"):
                    members.append((n, z.read(n)))
    else:
        members.append((path.name, path.read_bytes()))
    for name, blob in members:
        low = name.lower()
        if low.endswith(".xlsx"):
            _take_table(_xlsx_rows(blob), out)
        elif low.endswith(".csv"):
            rows = list(csv.reader(io.StringIO(blob.decode("utf-8", "replace"))))
            _take_table(rows, out)
        elif low.endswith(".txt") and "anweb" in low:
            # fixed width: code in columns 1-5, long description from column 12
            for line in blob.decode("latin-1").splitlines():
                code = line[:5].strip().upper()
                if CPT_RE.match(code) and len(line) > 11:
                    desc = _clean(line[11:91])
                    if desc and code not in out:
                        out[code] = desc
    return out


def _xlsx_rows(blob: bytes) -> list[list]:
    """Read an .xlsx held in memory: the reader wants a path, so use the system temp dir (never data/raw, which may be read-only)."""
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as fh:
        fh.write(blob)
        tmp = Path(fh.name)
    try:
        return list(xlsx.rows(tmp))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _take_table(rows: list[list], out: dict[str, str]) -> None:
    if not rows:
        return
    hdr = None
    for i, r in enumerate(rows[:10]):
        cells = [_clean(c).upper() for c in r]
        if any(c in ("HCPC", "HCPCS", "HCPCS CODE", "CODE") for c in cells):
            hdr = (i, cells)
            break
    if hdr is None:
        return
    i, cells = hdr
    ci = next(k for k, c in enumerate(cells) if c in ("HCPC", "HCPCS", "HCPCS CODE", "CODE"))
    di = next((k for k, c in enumerate(cells) if "LONG" in c), None)
    if di is None:
        di = next((k for k, c in enumerate(cells) if "DESC" in c), None)
    if di is None:
        return
    for r in rows[i + 1:]:
        if len(r) <= max(ci, di):
            continue
        code = _clean(r[ci]).upper()
        desc = _clean(r[di])
        if CPT_RE.match(code) and desc and code not in out:
            out[code] = desc


def load_msdrg_table(path: Path) -> dict[str, str]:
    """
    The MS-DRG definitions table (CMS IPPS final rule Table 5, or any CSV/xlsx
    with an MS-DRG column and a title column). Returns {drg: title}.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    blobs: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith((".xlsx", ".csv", ".txt")) and not n.startswith("__MACOSX"):
                    blobs.append((n, z.read(n)))
    else:
        blobs.append((path.name, path.read_bytes()))
    for name, blob in blobs:
        if name.lower().endswith(".xlsx"):
            rows = _xlsx_rows(blob)
        else:
            rows = list(csv.reader(io.StringIO(blob.decode("utf-8", "replace"))))
        hdr = None
        for i, r in enumerate(rows[:15]):
            cells = [_clean(c).upper() for c in r]
            if any(c.startswith("MS-DRG") or c == "DRG" for c in cells) and any("TITLE" in c or "DESC" in c for c in cells):
                hdr = (i, cells)
                break
        if hdr is None:
            continue
        i, cells = hdr
        ci = next(k for k, c in enumerate(cells) if c.startswith("MS-DRG") or c == "DRG")
        di = next(k for k, c in enumerate(cells) if "TITLE" in c or "DESC" in c)
        for r in rows[i + 1:]:
            if len(r) <= max(ci, di):
                continue
            code = _clean(r[ci]).split(".")[0].zfill(3)
            desc = _clean(r[di])
            if DRG_RE.match(code) and desc and code not in out:
                out[code] = desc
    return out


# ---------------------------------------------------------------------------
# the dictionary

def build(physician: Iterable[dict], inpatient: Iterable[dict], charges: Iterable[dict],
          basket: list[dict], hcpcs: Optional[dict[str, str]] = None,
          msdrg: Optional[dict[str, str]] = None) -> list[dict]:
    """
    One row per (code_type, code) seen in any source. Status and description
    follow the ladders in the module docstring.
    """
    hcpcs = hcpcs or {}
    msdrg = msdrg or {}
    cms_cpt: dict[str, str] = {}
    cms_states: dict[tuple[str, str], set] = defaultdict(set)
    for r in physician:
        code = (r.get("hcpcs") or "").strip().upper()
        if not code:
            continue
        cms_states[("CPT", code)].add(r.get("geo"))
        if r.get("hcpcs_desc") and code not in cms_cpt:
            cms_cpt[code] = _clean(r["hcpcs_desc"])
    cms_drg: dict[str, str] = {}
    for r in inpatient:
        code = (r.get("drg") or "").strip().zfill(3)
        cms_states[("MS-DRG", code)].add("*")
        if r.get("drg_desc") and code not in cms_drg:
            cms_drg[code] = _clean(r["drg_desc"])

    hosp_files: dict[tuple[str, str], set] = defaultdict(set)
    hosp_desc: dict[tuple[str, str], Counter] = defaultdict(Counter)   # lower-cased wording → files using it
    hosp_orig: dict[tuple[str, str, str], str] = {}                    # first original casing of that wording
    seen_pair: set = set()
    for c in charges:
        k = (c["code_type"], c["code"])
        hosp_files[k].add(c["seed_id"])
        d = _clean(c.get("description"))
        if d and (k, c["seed_id"], d.lower()) not in seen_pair:
            seen_pair.add((k, c["seed_id"], d.lower()))
            hosp_desc[k][d.lower()] += 1
            hosp_orig.setdefault((k[0], k[1], d.lower()), d)

    in_basket = {(b["type"], b["code"]): b for b in basket}
    keys = set(cms_states) | set(hosp_files) | set(in_basket)
    rows = []
    for ctype, code in sorted(keys):
        wf = well_formed(ctype, code)
        official = (ctype == "CPT" and (code in cms_cpt or code in hcpcs or ("CPT", code) in cms_states)) \
            or (ctype == "MS-DRG" and (code in cms_drg or code in msdrg))
        n_files = len(hosp_files.get((ctype, code), ()))
        if official:
            status = STATUS_OFFICIAL
        elif wf and n_files >= 2:
            status = STATUS_HOSPITAL
        else:
            status = STATUS_UNVERIFIED
        desc, src, desc_n = None, None, None
        if ctype == "CPT" and code in cms_cpt:
            desc, src = cms_cpt[code], "cms"
        elif ctype == "MS-DRG" and code in cms_drg:
            desc, src = cms_drg[code], "cms"
        elif ctype == "CPT" and code in hcpcs:
            desc, src = hcpcs[code], "hcpcs"
        elif ctype == "MS-DRG" and code in msdrg:
            desc, src = msdrg[code], "msdrg"
        elif (ctype, code) in hosp_desc:
            # the wording most hospitals used; ties go to the shorter string
            best, n = max(hosp_desc[(ctype, code)].items(), key=lambda kv: (kv[1], -len(kv[0])))
            desc, src, desc_n = hosp_orig[(ctype, code, best)], "hospital", n
        b = in_basket.get((ctype, code))
        if desc is None and b:
            desc, src = b["label"], "basket"   # basket.py's plain-language label
        # every other wording the hospitals used, most common first — for search
        # recall only (a CMS short descriptor says "Tot knee arthroplasty"; a
        # hospital says "TOTAL KNEE REPLACEMENT"). Never shown as the definition.
        alts = []
        if (ctype, code) in hosp_desc:
            for wording, _n in hosp_desc[(ctype, code)].most_common(8):
                orig = hosp_orig[(ctype, code, wording)]
                if orig != desc and orig.lower() != (desc or "").lower():
                    alts.append(orig)
        alt_text = " | ".join(alts)[:500] or None
        st = cms_states.get((ctype, code), set())
        rows.append({
            "code_type": ctype,
            "code": code,
            "status": status,
            "well_formed": wf,
            "description": desc,
            "desc_source": src,
            "desc_n": desc_n,
            "alt_descriptions": alt_text,
            "hospitals_n": n_files,
            "in_basket": bool(b),
            "basket_label": b["label"] if b else None,
            "basket_group": b["group"] if b else None,
            "in_medicare": bool(st),
            "medicare_states": len([s for s in st if s not in ("US", "*")]) if ctype == "CPT" else None,
        })
    return rows


def summary(rows: list[dict]) -> dict:
    by = Counter((r["code_type"], r["status"]) for r in rows)
    src = Counter(r["desc_source"] or "none" for r in rows)
    return {
        "codes": len(rows),
        "by_status": {f"{t}:{s}": n for (t, s), n in sorted(by.items())},
        "by_desc_source": dict(src),
        "cpt_shaped_strings_from_hospitals": sum(1 for r in rows if r["code_type"] == "CPT" and r["hospitals_n"]),
        "official_cpt": sum(1 for r in rows if r["code_type"] == "CPT" and r["status"] == STATUS_OFFICIAL),
        "official_drg": sum(1 for r in rows if r["code_type"] == "MS-DRG" and r["status"] == STATUS_OFFICIAL),
    }
