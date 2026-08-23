"""
Hospital price-transparency machine-readable files, sampled.

Since 2021 every US hospital must publish a machine-readable file of its
standard charges (45 CFR 180), and since 2024 must point to it from a
`cms-hpt.txt` at the root of its public domain. The files are enormous, vary
in shape, and — like the prior authorization metrics — are never checked by
the regulator for whether the numbers inside agree with each other.

This module takes a seed list of hospitals (data/seeds/hospitals_mrf.csv),
finds each one's file through cms-hpt.txt, streams it, keeps every item that
carries a recognisable CPT/HCPCS or MS-DRG code — the code systems the
Medicare files also speak, so every kept row can in principle be compared —
and records what happened at every step. A hospital whose file
cannot be found, fetched or parsed is a recorded gap with a reason, in the
same categories the prior-auth collection uses: `blocked` (the file exists and
resisted reading), `missing` (nothing was found) and `tool_limited` (this
crawler gave up — a size cap, a timeout). Only `blocked` says anything about
the hospital.

The CMS template, versions 2.0–2.2 (the shapes this parser understands):

  CSV   row 1–2: file metadata (hospital_name, last_updated_on, version, ...)
        row 3:   column headers
        "tall":  one row per item per payer/plan
        "wide":  one row per item, one column group per payer|plan
  JSON  {"hospital_name", "last_updated_on", "version", ...,
         "standard_charge_information": [{"code_information": [...],
                                          "standard_charges": [{"setting", "gross_charge",
                                          "discounted_cash", "minimum", "maximum",
                                          "payers_information": [...]}]}]}
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import statistics
import time
import zipfile
from pathlib import Path
from typing import Optional

from . import fetch

SEEDS = Path(__file__).resolve().parents[2] / "data" / "seeds" / "hospitals_mrf.csv"

CSV_MAX_BYTES = 1_500_000_000   # streamed, so this is a time cap more than a memory one
JSON_MAX_BYTES = 300_000_000    # json.load holds the whole document (~10x in RAM); past this it is a
                                # gap for scripts/recover_browser.py to stream with ijson (laptop RAM)

_CODE_TYPE = {
    "CPT": "CPT", "HCPCS": "CPT", "CPT/HCPCS": "CPT", "HCPCS/CPT": "CPT",
    "MS-DRG": "MS-DRG", "MSDRG": "MS-DRG", "DRG": "MS-DRG", "MS DRG": "MS-DRG",
}


def _money(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if s == "" or s.upper() in ("N/A", "NA", "NULL", "-", "--", "NONE"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f >= 0 else None


# A well-formed CPT (5 digits, or 4 digits + a category letter) or HCPCS
# level II code (a letter + 4 digits). Anything else under a CPT/HCPCS type
# label — CDM fragments, ranges, free text — is not a code in the books.
_CPT_RE = re.compile(r"^(?:\d{5}|\d{4}[A-Za-z]|[A-Za-z]\d{4})$")


def _recognize(code: str, ctype: str) -> Optional[tuple[str, str]]:
    """Every code in the books: any valid CPT/HCPCS or MS-DRG code, not a fixed list."""
    t = _CODE_TYPE.get((ctype or "").strip().upper())
    c = (code or "").strip()
    if t == "CPT" and _CPT_RE.match(c):
        return ("CPT", c.upper())
    if t == "MS-DRG":
        c = c.upper().replace("MS-DRG", "").replace("DRG", "").strip().lstrip("0") or "0"
        if c.isdigit() and len(c) <= 3:
            return ("MS-DRG", c.zfill(3))
    return None


# ---------------------------------------------------------------------------
# seeds and discovery

def load_seeds(path: Path = SEEDS) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def parse_hpt(text: str) -> list[dict]:
    """cms-hpt.txt: blank-line-separated blocks of `key: value`."""
    blocks: list[dict] = []
    cur: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            cur[k.strip().lower()] = v.strip()
    if cur:
        blocks.append(cur)
    # UT Southwestern files its CSV under source-page-url and omits mrf-url
    # entirely. When the "source page" is plainly the document, take it.
    _doc = re.compile(r"\.(csv|json|zip|gz|xlsx)(\?|$)|standardcharges", re.I)
    for b in blocks:
        if not b.get("mrf-url") and _doc.search(b.get("source-page-url", "")):
            b["mrf-url"] = b["source-page-url"]
    return [b for b in blocks if b.get("mrf-url")]


def _tokens(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    stop = {"the", "of", "and", "hospital", "medical", "center", "health", "inc", "llc",
            "university", "regional", "system", "campus", "a", "at", "dba"}
    return {t for t in s.split() if t and t not in stop}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def choose_block(blocks: list[dict], hospital: str) -> tuple[dict, float]:
    best, score = blocks[0], -1.0
    for b in blocks:
        s = _similarity(b.get("location-name", ""), hospital)
        if s > score:
            best, score = b, s
    return best, score


def discover(seed: dict, log=print) -> dict:
    """Find the MRF URL for one seed. Returns a partial mrf_file record."""
    rec = {
        "seed_id": seed["seed_id"], "state": seed["state"], "hospital": seed["hospital"],
        "domain": seed["domain"], "ccn": (seed.get("ccn") or "").strip() or None,
        "hpt_url": None, "hpt_status": None, "locations_listed": 0,
        "mrf_url": None, "location_name": None, "match_score": None,
    }
    # A seed can pin the MRF URL outright — for hosts whose cms-hpt.txt only a
    # real browser can read (bot walls, brotli-only responses). The pin was
    # found by hand in a browser; the record says so.
    pinned = (seed.get("mrf_url") or "").strip()
    if pinned:
        rec["mrf_url"] = pinned
        rec["hpt_status"] = "pinned (cms-hpt.txt unreadable to a crawler; URL found in a browser)"
        rec["location_name"] = (seed.get("location") or "").strip() or seed["hospital"]
        rec["match_score"] = None
        return rec
    # A seed can also pin the location-name to match, for hpt files where the
    # hospital's listed name shares no tokens with its common name.
    want = (seed.get("location") or "").strip() or seed["hospital"]
    tried = []
    for host in (seed["domain"], "www." + seed["domain"]) if not seed["domain"].startswith("www.") else (seed["domain"],):
        url = f"https://{host}/cms-hpt.txt"
        tried.append(url)
        try:
            body = fetch.text(url, timeout=45)
        except fetch.FetchError as e:
            rec["hpt_status"] = str(e)
            continue
        blocks = parse_hpt(body)
        rec["hpt_url"] = url
        if not blocks:
            rec["hpt_status"] = "fetched, but no mrf-url entries (HTML page or empty file)"
            continue
        rec["hpt_status"] = "ok" if fetch.text.last_profile == "crossfoot" else "ok (browser profile)"
        rec["locations_listed"] = len(blocks)
        block, score = choose_block(blocks, want)
        rec["mrf_url"] = block["mrf-url"]
        rec["location_name"] = block.get("location-name")
        rec["match_score"] = round(score, 2)
        return rec
    rec["hpt_tried"] = tried
    return rec


# ---------------------------------------------------------------------------
# parsing

class _Agg:
    """Aggregate one (code_type, code, setting) across payer rows."""
    __slots__ = ("description", "gross", "cash", "min", "max", "neg", "pct_algo", "estimated",
                 "payer_rows", "gross_seen", "cash_seen", "min_seen", "max_seen")

    def __init__(self, description: str):
        self.description = description
        self.gross = self.cash = self.min = self.max = None
        self.neg: list[float] = []
        self.pct_algo = 0
        self.estimated = 0
        self.payer_rows = 0
        self.gross_seen: set = set()
        self.cash_seen: set = set()
        self.min_seen: set = set()
        self.max_seen: set = set()

    def item(self, gross, cash, mn, mx):
        for attr, val in (("gross", gross), ("cash", cash), ("min", mn), ("max", mx)):
            if val is not None:
                getattr(self, attr + "_seen").add(val)
                if getattr(self, attr) is None:
                    setattr(self, attr, val)

    def payer(self, dollar, pct, algo, est):
        self.payer_rows += 1
        if dollar is not None:
            self.neg.append(dollar)
        elif pct is not None or (algo or "").strip():
            self.pct_algo += 1
            if est is not None:
                self.estimated += 1

    def row(self, seed_id, ctype, code, setting) -> dict:
        neg = sorted(self.neg)
        return {
            "seed_id": seed_id, "code_type": ctype, "code": code, "setting": setting,
            "description": self.description[:220],
            "gross": self.gross, "cash": self.cash, "min": self.min, "max": self.max,
            "negotiated_n": len(neg),
            "negotiated_median": round(statistics.median(neg), 2) if neg else None,
            "negotiated_min": neg[0] if neg else None,
            "negotiated_max": neg[-1] if neg else None,
            "pct_or_algo_n": self.pct_algo,
            "estimated_n": self.estimated,
            "payer_rows": self.payer_rows,
            # more than one distinct value for an item-level field is itself a finding
            "gross_variants": len(self.gross_seen), "cash_variants": len(self.cash_seen),
            "min_variants": len(self.min_seen), "max_variants": len(self.max_seen),
        }


def _norm_setting(s) -> str:
    s = (s or "").strip().lower()
    return s if s in ("inpatient", "outpatient", "both") else (s or "unspecified")


def parse_csv(lines, seed_id: str, meta_out: dict) -> dict[tuple, _Agg]:
    """Stream the CMS CSV template (tall or wide). Fills meta_out; returns aggregates."""
    rdr = csv.reader(lines)
    try:
        meta_keys = next(rdr)
        meta_vals = next(rdr)
        header = next(rdr)
    except StopIteration:
        raise ValueError("file has fewer than three rows")
    meta = {k.strip().lower(): (meta_vals[i].strip() if i < len(meta_vals) else "")
            for i, k in enumerate(meta_keys)}
    meta_out.update({
        "hospital_name": meta.get("hospital_name"),
        "last_updated_on": meta.get("last_updated_on"),
        "version": meta.get("version"),
        "affirmation": meta.get("affirmation") or None,
    })
    def _norm_hdr(cells) -> list[str]:
        # Some hospitals write "code | 1 | type" with spaces around the pipes.
        return [re.sub(r"\s*\|\s*", "|", h.strip()) for h in cells]

    hdr = _norm_hdr(header)
    if "description" not in [h.lower() for h in hdr] or not any(h.lower().startswith("code|") for h in hdr):
        # Some hospitals omit the two metadata rows and start with the header.
        probe = _norm_hdr(meta_keys)
        if "description" in [h.lower() for h in probe] and any(h.lower().startswith("code|") for h in probe):
            hdr = probe
            # rows 2 and 3 were data; they are lost to the header probe — note it
            meta_out["note"] = "no metadata rows; header on row 1 (two data rows skipped)"
            meta_out["hospital_name"] = meta_out["hospital_name"] or None
        else:
            raise ValueError(f"not the CMS template: header {hdr[:8]}")
    col = {h.lower(): i for i, h in enumerate(hdr)}

    code_cols = []
    for n in range(1, 12):
        c, t = f"code|{n}", f"code|{n}|type"
        if c in col and t in col:
            code_cols.append((col[c], col[t]))
    if not code_cols:
        raise ValueError(f"no code|N / code|N|type columns in header {hdr[:12]}")

    def g(row, key):
        i = col.get(key)
        return row[i] if i is not None and i < len(row) else None

    tall = "payer_name" in col
    wide_groups: list[tuple[str, int, Optional[int], Optional[int], Optional[int]]] = []
    if not tall:
        for h, i in col.items():
            m = re.match(r"standard_charge\|(.+)\|negotiated_dollar$", h)
            if m:
                payer = m.group(1)
                wide_groups.append((
                    payer, i,
                    col.get(f"standard_charge|{payer}|negotiated_percentage"),
                    col.get(f"standard_charge|{payer}|negotiated_algorithm"),
                    col.get(f"estimated_amount|{payer}"),
                ))
    meta_out["format"] = "csv-tall" if tall else ("csv-wide" if wide_groups else "csv-unknown")
    meta_out["payer_columns"] = len(wide_groups)

    aggs: dict[tuple, _Agg] = {}
    n = 0
    for row in rdr:
        n += 1
        hit = None
        for ci, ti in code_cols:
            if ci < len(row) and ti < len(row):
                hit = _recognize(row[ci], row[ti])
                if hit:
                    break
        if not hit:
            continue
        ctype, code = hit
        setting = _norm_setting(g(row, "setting"))
        if ctype == "MS-DRG" and setting == "outpatient":
            continue
        if ctype == "CPT" and setting == "inpatient":
            continue
        key = (ctype, code, setting)
        a = aggs.get(key)
        if a is None:
            a = aggs[key] = _Agg(g(row, "description") or "")
        a.item(_money(g(row, "standard_charge|gross")), _money(g(row, "standard_charge|discounted_cash")),
               _money(g(row, "standard_charge|min")), _money(g(row, "standard_charge|max")))
        if tall:
            a.payer(_money(g(row, "standard_charge|negotiated_dollar")),
                    _money(g(row, "standard_charge|negotiated_percentage")),
                    g(row, "standard_charge|negotiated_algorithm"),
                    _money(g(row, "estimated_amount")))
        else:
            for _, di, pi, ai, ei in wide_groups:
                dollar = _money(row[di]) if di < len(row) else None
                pct = _money(row[pi]) if pi is not None and pi < len(row) else None
                algo = row[ai] if ai is not None and ai < len(row) else None
                est = _money(row[ei]) if ei is not None and ei < len(row) else None
                if dollar is None and pct is None and not (algo or "").strip():
                    continue
                a.payer(dollar, pct, algo, est)
    meta_out["rows_scanned"] = n
    return aggs


def parse_json(doc: dict, seed_id: str, meta_out: dict) -> dict[tuple, _Agg]:
    meta_out.update({
        "hospital_name": doc.get("hospital_name"),
        "last_updated_on": doc.get("last_updated_on"),
        "version": doc.get("version"),
        "affirmation": (doc.get("affirmation") or {}).get("affirmation") if isinstance(doc.get("affirmation"), dict) else None,
        "format": "json",
    })
    items = doc.get("standard_charge_information") or []
    aggs: dict[tuple, _Agg] = {}
    for item in items:
        hit = None
        for ci in item.get("code_information") or []:
            hit = _recognize(str(ci.get("code", "")), str(ci.get("type", "")))
            if hit:
                break
        if not hit:
            continue
        ctype, code = hit
        for sc in item.get("standard_charges") or []:
            setting = _norm_setting(sc.get("setting"))
            if ctype == "MS-DRG" and setting == "outpatient":
                continue
            if ctype == "CPT" and setting == "inpatient":
                continue
            key = (ctype, code, setting)
            a = aggs.get(key)
            if a is None:
                a = aggs[key] = _Agg(item.get("description") or "")
            a.item(_money(sc.get("gross_charge")), _money(sc.get("discounted_cash")),
                   _money(sc.get("minimum")), _money(sc.get("maximum")))
            for p in sc.get("payers_information") or []:
                a.payer(_money(p.get("standard_charge_dollar")), _money(p.get("standard_charge_percentage")),
                        p.get("standard_charge_algorithm"), _money(p.get("estimated_amount")))
    meta_out["rows_scanned"] = len(items)
    return aggs


# ---------------------------------------------------------------------------

def _zip_member(zf: zipfile.ZipFile, hospital: str) -> zipfile.ZipInfo:
    """The document inside a zip MRF: the best name match to the hospital
    among .csv/.json members, else the largest file."""
    members = [m for m in zf.infolist() if not m.is_dir() and m.file_size > 0]
    if not members:
        raise ValueError("zip archive holds no files")
    docs = [m for m in members if re.search(r"\.(csv|json)$", m.filename, re.I)] or members
    if len(docs) == 1:
        return docs[0]
    scored = [(m, _similarity(m.filename.replace("_", " ").replace("-", " "), hospital)) for m in docs]
    best, score = max(scored, key=lambda t: t[1])
    return best if score > 0 else max(docs, key=lambda m: m.file_size)


def _classify_failure(reason: str) -> str:
    r = reason.lower()
    if r.startswith("http 403") or "bot" in r or "forbidden" in r or "captcha" in r:
        return "blocked"
    if "too large" in r or "timed out" in r or "timeout" in r:
        return "tool_limited"
    if "not the cms template" in r or "no code|" in r or "json" in r and "decode" in r:
        return "blocked"          # a file exists and cannot be read: that is about the hospital
    if r.startswith("http 404") or "unreachable" in r or "no mrf-url" in r or "name or service" in r:
        return "missing"
    return "missing"


def collect_one(seed: dict, log=print, csv_max=CSV_MAX_BYTES, json_max=JSON_MAX_BYTES,
                sleep: float = 1.0) -> tuple[dict, list[dict]]:
    """Discover, fetch and parse one hospital's MRF. Always returns a file record; charges may be empty."""
    rec = discover(seed, log=log)
    rec.update({"format": None, "hospital_name": None, "last_updated_on": None, "version": None,
                "affirmation": None, "bytes": None, "sha256": None, "rows_scanned": None,
                "basket_items": 0, "status": None, "gap_group": None, "reason": None,
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if not rec["mrf_url"]:
        rec["status"] = "missing"
        rec["reason"] = f"cms-hpt.txt: {rec['hpt_status']}"
        rec["gap_group"] = _classify_failure(rec["reason"])
        log(f"  {seed['seed_id']:6} {seed['hospital'][:40]:40} — {rec['reason']}")
        return rec, []

    url = rec["mrf_url"]
    charges: list[dict] = []
    try:
        info = {}
        try:
            info = fetch.head(url, timeout=45)
        except fetch.FetchError as e:
            info = {"type": "", "length": 0, "note": str(e)}
        ctype = (info.get("type") or "").lower()
        upath = url.lower().split("?")[0]
        if upath.endswith(".gz"):
            upath = upath[:-3]
        # What the URL claims is unreliable: JSON is served from .ashx and
        # extension-less endpoints, and "csv" links deliver zip archives.
        # Sniff the first bytes and believe them; fall back to the URL and
        # content type only when nothing could be sniffed.
        probe = fetch.sniff(url).lstrip(b"\xef\xbb\xbf \t\r\n")
        # gzip magic in the probe is transfer compression (some hosts apply it
        # even to a ranged request); the URL/content type still names the real
        # container underneath.
        opaque = not probe or probe[:2] == b"\x1f\x8b"
        is_zip = probe[:4] == b"PK\x03\x04" or (opaque and (upath.endswith(".zip") or "zip" in ctype))
        if probe and probe[:2] != b"\x1f\x8b" and not is_zip:
            is_json = probe[:1] in (b"{", b"[")
        else:
            is_json = upath.endswith(".json") or "json" in ctype
        meta: dict = {}
        if is_zip:
            path = fetch.download(url, f"mrf_{seed['seed_id']}.zip", timeout=900, max_bytes=csv_max)
            src = fetch.source_record(f"mrf_{seed['seed_id']}.zip") or {}
            rec["bytes"], rec["sha256"] = src.get("bytes"), src.get("sha256")
            try:
                with zipfile.ZipFile(path) as zf:
                    m = _zip_member(zf, seed["hospital"])
                    with zf.open(m) as fh:
                        mhead = fh.read(64).lstrip(b"\xef\xbb\xbf \t\r\n")
                    if mhead[:1] in (b"{", b"[") or m.filename.lower().endswith(".json"):
                        if m.file_size > json_max:
                            raise fetch.FetchError(
                                f"too large: zip member {m.filename} is "
                                f"{m.file_size:,} bytes uncompressed (cap {json_max:,})")
                        with zf.open(m) as fh:
                            doc = json.load(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))
                        if isinstance(doc, list):
                            doc = doc[0] if doc and isinstance(doc[0], dict) else {}
                        aggs = parse_json(doc, seed["seed_id"], meta)
                    else:
                        with zf.open(m) as fh:
                            aggs = parse_csv(
                                io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline=""),
                                seed["seed_id"], meta)
                    meta["note"] = ((meta.get("note") + "; ") if meta.get("note") else "") \
                        + f"zip archive; member {m.filename}"
            finally:
                path.unlink(missing_ok=True)  # the coded rows are kept; the archive is not
        elif is_json:
            if info.get("length", 0) > json_max:
                raise fetch.FetchError(f"too large: JSON of {info['length']:,} bytes (cap {json_max:,})")
            path = fetch.download(url, f"mrf_{seed['seed_id']}.json", timeout=900, max_bytes=json_max)
            src = fetch.source_record(f"mrf_{seed['seed_id']}.json") or {}
            rec["bytes"], rec["sha256"] = src.get("bytes"), src.get("sha256")
            # a .json.gz payload is still the template, one unwrap away
            with open(path, "rb") as fh:
                opener = gzip.open if fh.read(2) == b"\x1f\x8b" else open
            with opener(path, "rb") as fh:
                head = fh.read(1024)
            if head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] not in (b"{", b"["):
                raise ValueError("not the CMS template: body does not begin with a JSON object")
            with opener(path, "rt", encoding="utf-8-sig", errors="replace") as fh:
                doc = json.load(fh)
            if isinstance(doc, list):
                doc = doc[0] if doc and isinstance(doc[0], dict) else {}
            aggs = parse_json(doc, seed["seed_id"], meta)
            path.unlink(missing_ok=True)  # the basket rows are kept; the 100 MB original is not
        else:
            sha = hashlib.sha256()
            def lines():
                for line in fetch.stream_lines(url, timeout=1800, max_bytes=csv_max):
                    sha.update(line.encode("utf-8", "replace"))
                    yield line
            aggs = parse_csv(lines(), seed["seed_id"], meta)
            m = getattr(fetch.stream_lines, "last_meta", {})
            rec["bytes"], rec["sha256"] = m.get("bytes"), sha.hexdigest()
        rec.update({k: meta.get(k) for k in ("format", "hospital_name", "last_updated_on",
                                             "version", "affirmation", "rows_scanned")})
        if meta.get("note"):
            rec["reason"] = meta["note"]
        for (ct, code, setting), a in sorted(aggs.items()):
            charges.append(a.row(seed["seed_id"], ct, code, setting))
        # named for the original basket scan; since the every-code change it
        # counts every coded item kept. The schema contract keeps the name.
        rec["basket_items"] = len(charges)
        rec["status"] = "ok" if charges else "parsed_empty"
        if not charges:
            rec["reason"] = (rec["reason"] or "") + " parsed, but no CPT/HCPCS or MS-DRG codes were recognised"
            rec["gap_group"] = "blocked"
        log(f"  {seed['seed_id']:6} {seed['hospital'][:40]:40} {rec['format'] or '?':9} "
            f"{(rec['bytes'] or 0)/1e6:8.1f} MB  {len(charges):6,} coded items")
    except (fetch.FetchError, ValueError, json.JSONDecodeError, UnicodeDecodeError,
            MemoryError, zipfile.BadZipFile, OSError) as e:
        reason = str(e) if not isinstance(e, json.JSONDecodeError) else f"JSON decode error: {e}"
        rec["status"] = "failed"
        rec["reason"] = reason
        rec["gap_group"] = _classify_failure(reason)
        log(f"  {seed['seed_id']:6} {seed['hospital'][:40]:40} — {reason[:90]}")
    time.sleep(sleep)
    return rec, charges


def match_ccn(rec: dict, hospitals: dict[str, dict]) -> None:
    """Attach the Medicare CCN so MRF prices can sit beside Medicare charges for the same hospital."""
    if rec.get("ccn"):
        rec["ccn_match"] = "seed"
        return
    name = rec.get("hospital_name") or rec.get("location_name") or rec["hospital"]
    best, score = None, 0.0
    for ccn, h in hospitals.items():
        if h.get("state") != rec["state"]:
            continue
        s = max(_similarity(name, h["name"]), _similarity(rec["hospital"], h["name"]))
        if s > score:
            best, score = ccn, s
    if best and score >= 0.5:
        rec["ccn"], rec["ccn_match"] = best, f"name ({score:.2f})"
    else:
        rec["ccn_match"] = "unmatched"
