#!/usr/bin/env python3
"""
Manual recovery for seeds whose cms-hpt.txt a Python client cannot reach.

IN-01 (IU Health Methodist): iuhealth.org/cms-hpt.txt hangs for non-browser
clients, but a real browser is redirected to cdn.iuhealth.org/global/cms-hpt.txt,
which serves normally. The listed MRF is a .zip (the crawler unwraps .gz, not
.zip), so the inner CSV/JSON is extracted here and parsed with the pipeline's
own parsers. Provenance: bytes/sha256 recorded are those of the zip as served.

Run after harvest_large.py (they share out/prices/mrf_*.json):
  venv/bin/python recover_manual.py
"""
import io
import json
import hashlib
import sys
import time
import zipfile
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
SCRATCH = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE))
sys.path.insert(0, str(SCRATCH))

from pipeline.prices import fetch, mrf  # noqa: E402
from harvest_large import parse_json_stream, log  # noqa: E402

OUT = PIPELINE / "out" / "prices"

# seed_id -> how to reach what the automated crawl could not
MANUAL = {
    "IN-01": {
        "hpt_url": "https://iuhealth.org/cms-hpt.txt",
        "hpt_file": SCRATCH / "iuhealth_cms-hpt.txt",
        "note": "cms-hpt.txt hangs for non-browser clients; read via its cdn.iuhealth.org redirect",
    },
    "NC-03": {
        "hpt_url": "https://unchealth.org/cms-hpt.txt",
        "hpt_file": SCRATCH / "unc_cms-hpt.txt",
        # the tokenizer drops medical/center as stopwords, so "UNC Health
        # Rockingham" tied "UNC Hospitals" at 0.5 and the earlier block won;
        # UNC Medical Center's legal location name is "UNC Hospitals"
        "block_name": "UNC Hospitals",
        # their cms-hpt.txt lists http://, but the host filters port 80
        "force_https": True,
        "note": "location block chosen by hand (UNC Hospitals = UNC Medical Center; "
                "the name matcher picked UNC Health Rockingham); mrf-url rewritten to "
                "https, the listed http endpoint does not answer",
    },
}


def recover(seed):
    sid = seed["seed_id"]
    m = MANUAL[sid]
    hpt_url, note = m["hpt_url"], m["note"]
    blocks = mrf.parse_hpt(m["hpt_file"].read_text(encoding="utf-8"))
    if m.get("block_name"):
        block = next(b for b in blocks if b.get("location-name") == m["block_name"])
        score = mrf._similarity(block.get("location-name", ""), seed["hospital"])
    else:
        block, score = mrf.choose_block(blocks, seed["hospital"])
    if m.get("force_https") and block["mrf-url"].startswith("http://"):
        block = dict(block, **{"mrf-url": "https://" + block["mrf-url"][7:]})
    rec = {
        "seed_id": sid, "state": seed["state"], "hospital": seed["hospital"],
        "domain": seed["domain"], "ccn": (seed.get("ccn") or "").strip() or None,
        "hpt_url": hpt_url, "hpt_status": "ok", "locations_listed": len(blocks),
        "mrf_url": block["mrf-url"], "location_name": block.get("location-name"),
        "match_score": round(score, 2),
        "format": None, "hospital_name": None, "last_updated_on": None, "version": None,
        "affirmation": None, "bytes": None, "sha256": None, "rows_scanned": None,
        "basket_items": 0, "status": None, "gap_group": None, "reason": None,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    url = rec["mrf_url"]
    charges = []
    try:
        name = f"mrf_manual_{sid}" + (".zip" if url.lower().endswith(".zip") else "")
        path = fetch.download(url, name, refresh=True, timeout=3600, max_bytes=None)
        src = fetch.source_record(name) or {}
        rec["bytes"], rec["sha256"] = src.get("bytes"), src.get("sha256")
        log(f"  {sid} downloaded {(rec['bytes'] or 0)/1e6:,.0f} MB")
        meta = {}
        if url.lower().endswith(".zip"):
            zf = zipfile.ZipFile(path)
            inner = [i for i in zf.infolist() if not i.is_dir()]
            inner.sort(key=lambda i: i.file_size, reverse=True)
            member = inner[0]
            log(f"  {sid} zip contains {[i.filename for i in inner]}; parsing {member.filename}")
            if member.filename.lower().endswith(".json"):
                with zf.open(member) as fh:
                    buf = io.BufferedReader(fh, 1 << 20)
                    if buf.peek(3)[:3] == b"\xef\xbb\xbf":
                        buf.read(3)
                    aggs = parse_json_stream(buf, meta)
            else:
                with zf.open(member) as fh:
                    wrapper = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline="")
                    aggs = mrf.parse_csv(wrapper, sid, meta)
        else:
            with open(path, "rb") as fh:
                wrapper = io.TextIOWrapper(io.BufferedReader(fh, 1 << 20),
                                           encoding="utf-8-sig", errors="replace", newline="")
                aggs = mrf.parse_csv(wrapper, sid, meta)
        path.unlink(missing_ok=True)
        Path(str(path) + ".SOURCE.json").unlink(missing_ok=True)
        rec.update({k: meta.get(k) for k in ("format", "hospital_name", "last_updated_on",
                                             "version", "affirmation", "rows_scanned")})
        rec["reason"] = note if not meta.get("note") else f"{note}; {meta['note']}"
        for (ct, code, setting), a in sorted(aggs.items()):
            charges.append(a.row(sid, ct, code, setting))
        rec["basket_items"] = len(charges)
        rec["status"] = "ok" if charges else "parsed_empty"
        if not charges:
            rec["reason"] += " parsed, but no CPT/HCPCS or MS-DRG codes were recognised"
            rec["gap_group"] = "blocked"
        log(f"  {sid} done: {rec['format']}, {rec['rows_scanned']:,} rows, {len(charges):,} coded items")
    except Exception as e:  # noqa: BLE001
        rec["status"] = "failed"
        rec["reason"] = f"{note}; {e}"
        rec["gap_group"] = mrf._classify_failure(str(e))
        log(f"  {sid} FAILED: {str(e)[:140]}")
    return rec, charges


def main():
    seeds = {s["seed_id"]: s for s in mrf.load_seeds()}
    hospitals = {h["ccn"]: h for h in json.loads((OUT / "hospitals.json").read_text())}
    for sid in MANUAL:
        log(f"── {sid} (manual) ──")
        rec, rows = recover(seeds[sid])
        mrf.match_ccn(rec, hospitals)
        files = [f for f in json.loads((OUT / "mrf_files.json").read_text()) if f["seed_id"] != sid] + [rec]
        files.sort(key=lambda f: f["seed_id"])
        charges = [c for c in json.loads((OUT / "mrf_charges.json").read_text()) if c["seed_id"] != sid] + rows
        (OUT / "mrf_files.json").write_text(json.dumps(files, indent=1))
        (OUT / "mrf_charges.json").write_text(json.dumps(charges, indent=1))


if __name__ == "__main__":
    main()
