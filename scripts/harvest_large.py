#!/usr/bin/env python3
"""
Recover the 13 tool_limited seeds in the hospital MRF sample.

Why they failed:
  - 10 files were bigger than the crawler's caps (CSV 1.5 GB streamed,
    JSON 600 MB because mrf.collect_one json.load()s the whole document).
  - 3 timed out (IN-01, MN-02 at cms-hpt.txt discovery; NC-03 unreachable).

What this does differently, and nothing else:
  - no size caps (CSV streamed uncapped; nothing is held in memory),
  - JSON is downloaded to data/raw and parsed *incrementally* with ijson,
    so a 5 GB document never becomes a Python object tree,
  - discovery/fetch timeouts raised, transient timeouts retried 3x.

Parsing semantics are the pipeline's own: parse_csv is reused verbatim, and
the streaming JSON path replicates mrf.parse_json item-for-item (same _Agg,
same _recognize/_money/_norm_setting, same setting filters, doc[0] for a
list-wrapped document). Records match collect_one's shape exactly.

After each seed the merged mrf_files.json / mrf_charges.json are written, so
the run is resumable: a seed whose current record no longer says
tool_limited is skipped.

Run with the venv python (needs ijson):
  venv/bin/python harvest_large.py
"""
import io
import json
import gzip
import hashlib
import sys
import time
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

import ijson  # noqa: E402
from ijson.common import ObjectBuilder  # noqa: E402
from pipeline.prices import fetch, mrf  # noqa: E402

OUT = PIPELINE / "out" / "prices"
SEED_IDS = ["CA-02", "IL-01", "IN-01", "IA-01", "KY-02", "MN-02", "NV-01",
            "NJ-01", "NC-02", "NC-03", "PA-01", "TN-01", "UT-01"]

# discovery uses fetch.text(url, timeout=45); IN-01 and MN-02 timed out there
_orig_text = fetch.text
def _patient_text(url, timeout=60, limit=4 << 20):
    return _orig_text(url, timeout=max(timeout, 240), limit=limit)
fetch.text = _patient_text
mrf.fetch.text = _patient_text


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _process_item(item, aggs):
    """One standard_charge_information item — the body of mrf.parse_json's loop."""
    if not isinstance(item, dict):
        return
    hit = None
    for ci in item.get("code_information") or []:
        hit = mrf._recognize(str(ci.get("code", "")), str(ci.get("type", "")))
        if hit:
            break
    if not hit:
        return
    ctype, code = hit
    for sc in item.get("standard_charges") or []:
        setting = mrf._norm_setting(sc.get("setting"))
        if ctype == "MS-DRG" and setting == "outpatient":
            continue
        if ctype == "CPT" and setting == "inpatient":
            continue
        key = (ctype, code, setting)
        a = aggs.get(key)
        if a is None:
            a = aggs[key] = mrf._Agg(item.get("description") or "")
        a.item(mrf._money(sc.get("gross_charge")), mrf._money(sc.get("discounted_cash")),
               mrf._money(sc.get("minimum")), mrf._money(sc.get("maximum")))
        for p in sc.get("payers_information") or []:
            a.payer(mrf._money(p.get("standard_charge_dollar")),
                    mrf._money(p.get("standard_charge_percentage")),
                    p.get("standard_charge_algorithm"),
                    mrf._money(p.get("estimated_amount")))


def parse_json_stream(fh, meta_out):
    """mrf.parse_json, but incremental: constant memory at any document size."""
    meta_out["format"] = "json"
    aggs, items, builder = {}, 0, None
    root = ""            # 'item.' when the document is a list; only doc[0] is read
    first = True
    last_beat = time.time()
    for prefix, event, value in ijson.parse(fh):
        if first:
            first = False
            if event == "start_array":
                root = "item."
                continue
        if root:
            if prefix == "item" and event == "end_map":
                break                      # doc[0] only, as json.load path does
            if prefix == "item" or prefix.startswith(root):
                prefix = prefix[len(root):] if prefix.startswith(root) else ""
            else:
                continue
        if builder is not None:
            builder.event(event, value)
            if prefix == "standard_charge_information.item" and event == "end_map":
                _process_item(builder.value, aggs)
                items += 1
                builder = None
                if items % 50000 == 0:
                    log(f"    … {items:,} items, {len(aggs):,} coded aggregates")
        elif prefix == "standard_charge_information.item" and event == "start_map":
            builder = ObjectBuilder()
            builder.event(event, value)
        elif event in ("string", "number", "boolean"):
            if prefix in ("hospital_name", "last_updated_on", "version"):
                meta_out[prefix] = value if isinstance(value, str) else str(value)
            elif prefix == "affirmation.affirmation":
                meta_out["affirmation"] = value
        if time.time() - last_beat > 300:
            last_beat = time.time()
            log(f"    … still parsing ({items:,} items so far)")
    meta_out["rows_scanned"] = items
    return aggs


def _retry(fn, what, tries=3):
    for i in range(tries):
        try:
            return fn()
        except fetch.FetchError as e:
            r = str(e).lower()
            transient = "timed out" in r or "timeout" in r or "unreachable" in r or r.startswith("http 5")
            if i + 1 == tries or not transient:
                raise
            log(f"    {what}: {e} — retry {i + 2}/{tries} in 60s")
            time.sleep(60)


def collect_uncapped(seed):
    """mrf.collect_one with the caps removed and JSON parsed incrementally."""
    rec = mrf.discover(seed)
    rec.pop("hpt_tried", None)
    rec.update({"format": None, "hospital_name": None, "last_updated_on": None, "version": None,
                "affirmation": None, "bytes": None, "sha256": None, "rows_scanned": None,
                "basket_items": 0, "status": None, "gap_group": None, "reason": None,
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if not rec["mrf_url"]:
        rec["status"] = "failed"
        rec["reason"] = f"cms-hpt.txt: {rec['hpt_status']}"
        rec["gap_group"] = mrf._classify_failure(rec["reason"])
        log(f"  {seed['seed_id']} — {rec['reason']}")
        return rec, []

    url = rec["mrf_url"]
    charges = []
    try:
        try:
            info = fetch.head(url, timeout=120)
        except fetch.FetchError as e:
            info = {"type": "", "length": 0, "note": str(e)}
        ctype = (info.get("type") or "").lower()
        upath = url.lower().split("?")[0]
        if upath.endswith(".gz"):
            upath = upath[:-3]
        is_json = upath.endswith(".json") or "json" in ctype
        declared = info.get("length") or 0
        log(f"  {seed['seed_id']} {seed['hospital'][:44]} — {'json' if is_json else 'csv'}, "
            f"{declared/1e9:.2f} GB declared" if declared else
            f"  {seed['seed_id']} {seed['hospital'][:44]} — {'json' if is_json else 'csv'}, size unknown")
        meta = {}
        if is_json:
            name = f"mrf_large_{seed['seed_id']}.json"
            path = _retry(lambda: fetch.download(url, name, refresh=True, timeout=3600, max_bytes=None),
                          "download")
            src = fetch.source_record(name) or {}
            rec["bytes"], rec["sha256"] = src.get("bytes"), src.get("sha256")
            log(f"    downloaded {(rec['bytes'] or 0)/1e9:.2f} GB; parsing …")
            with open(path, "rb") as fh:
                opener = gzip.open if fh.read(2) == b"\x1f\x8b" else open
            with opener(path, "rb") as fh:
                head = fh.read(1024)
            if head.lstrip()[:1] not in (b"{", b"["):
                raise ValueError("not the CMS template: body does not begin with a JSON object")
            with opener(path, "rb") as fh:
                buf = io.BufferedReader(fh, 1 << 20)
                if buf.peek(3)[:3] == b"\xef\xbb\xbf":
                    buf.read(3)
                aggs = parse_json_stream(buf, meta)
            path.unlink(missing_ok=True)
            Path(str(path) + ".SOURCE.json").unlink(missing_ok=True)
        else:
            sha = hashlib.sha256()
            def lines():
                for line in fetch.stream_lines(url, timeout=3600, max_bytes=None):
                    sha.update(line.encode("utf-8", "replace"))
                    yield line
            aggs = _retry(lambda: mrf.parse_csv(lines(), seed["seed_id"], meta), "stream")
            m = getattr(fetch.stream_lines, "last_meta", {})
            rec["bytes"], rec["sha256"] = m.get("bytes"), sha.hexdigest()
        rec.update({k: meta.get(k) for k in ("format", "hospital_name", "last_updated_on",
                                             "version", "affirmation", "rows_scanned")})
        if meta.get("note"):
            rec["reason"] = meta["note"]
        for (ct, code, setting), a in sorted(aggs.items()):
            charges.append(a.row(seed["seed_id"], ct, code, setting))
        rec["basket_items"] = len(charges)
        rec["status"] = "ok" if charges else "parsed_empty"
        if not charges:
            rec["reason"] = (rec["reason"] or "") + " parsed, but no CPT/HCPCS or MS-DRG codes were recognised"
            rec["gap_group"] = "blocked"
        log(f"  {seed['seed_id']} done: {rec['format']}, {(rec['bytes'] or 0)/1e6:,.0f} MB, "
            f"{rec['rows_scanned']:,} rows/items, {len(charges):,} coded items")
    except (fetch.FetchError, ValueError, json.JSONDecodeError, UnicodeDecodeError,
            MemoryError, ijson.JSONError) as e:
        rec["status"] = "failed"
        rec["reason"] = str(e)
        rec["gap_group"] = mrf._classify_failure(str(e))
        log(f"  {seed['seed_id']} FAILED: {str(e)[:140]}")
    return rec, charges


def main():
    seeds = {s["seed_id"]: s for s in mrf.load_seeds()}
    hospitals = {h["ccn"]: h for h in json.loads((OUT / "hospitals.json").read_text())}
    done, failed = [], []
    for sid in SEED_IDS:
        files = json.loads((OUT / "mrf_files.json").read_text())
        cur = next((f for f in files if f["seed_id"] == sid), None)
        if cur and cur.get("gap_group") != "tool_limited":
            log(f"  {sid} already recovered ({cur.get('status')}) — skipping")
            continue
        log(f"── {sid} ──")
        rec, rows = collect_uncapped(seeds[sid])
        mrf.match_ccn(rec, hospitals)
        # merge, exactly as build.py does for a partial run
        files = [f for f in files if f["seed_id"] != sid] + [rec]
        files.sort(key=lambda f: f["seed_id"])
        charges = json.loads((OUT / "mrf_charges.json").read_text())
        charges = [c for c in charges if c["seed_id"] != sid] + rows
        (OUT / "mrf_files.json").write_text(json.dumps(files, indent=1))
        (OUT / "mrf_charges.json").write_text(json.dumps(charges, indent=1))
        (done if rec["status"] == "ok" else failed).append(sid)
        time.sleep(2)
    log(f"finished: {len(done)} recovered {done}; {len(failed)} still failing {failed}")


if __name__ == "__main__":
    main()
