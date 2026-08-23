#!/usr/bin/env python3
"""
CA-06 (Highland Hospital, Alameda Health System): the host serves its leaf
certificate without the GoDaddy G2 intermediate, so every strict TLS client
fails verification. The fix is to complete the chain, not to skip it: the
intermediate is fetched from the CA Issuers URI in the leaf's AIA extension
and added to the trust store, and verification then passes normally.

  python3 scripts/recover_ca06.py /path/to/gdig2.pem
"""
import io
import json
import hashlib
import ssl
import sys
import time
import urllib.request
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))
from pipeline.prices import mrf  # noqa: E402

OUT = PIPELINE / "out" / "prices"
SID = "CA-06"
URL = ("https://www.alamedahealthsystem.org/wp-content/uploads/pricing/"
       "943302014_highland-hospital_standardcharges.csv")
NOTE = ("host omits its TLS intermediate certificate; chain completed from the "
        "leaf's AIA (GoDaddy G2) and verified normally")


def main():
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(sys.argv[1])
    seed = next(s for s in mrf.load_seeds() if s["seed_id"] == SID)
    rec = {
        "seed_id": SID, "state": seed["state"], "hospital": seed["hospital"],
        "domain": seed["domain"], "ccn": (seed.get("ccn") or "").strip() or None,
        "hpt_url": "https://www.alamedahealthsystem.org/cms-hpt.txt",
        "hpt_status": "ok", "locations_listed": 5,
        "mrf_url": URL, "location_name": "Highland Hospital", "match_score": None,
        "format": None, "hospital_name": None, "last_updated_on": None, "version": None,
        "affirmation": None, "bytes": None, "sha256": None, "rows_scanned": None,
        "basket_items": 0, "status": None, "gap_group": None, "reason": None,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    charges = []
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": mrf.fetch.UA})
        sha = hashlib.sha256()
        meta: dict = {}
        with urllib.request.urlopen(req, timeout=600, context=ctx) as r:
            raw = io.BufferedReader(r, 1 << 20)

            class Counting(io.RawIOBase):
                def __init__(self, fh):
                    self.fh = fh
                    self.n = 0

                def readable(self):
                    return True

                def readinto(self, b):
                    got = self.fh.readinto(b)
                    if got:
                        sha.update(bytes(b[:got]))
                        self.n += got
                    return got

            counting = Counting(raw)
            wrapper = io.TextIOWrapper(io.BufferedReader(counting, 1 << 20),
                                       encoding="utf-8-sig", errors="replace", newline="")
            aggs = mrf.parse_csv(wrapper, SID, meta)
            rec["bytes"], rec["sha256"] = counting.n, sha.hexdigest()
        rec.update({k: meta.get(k) for k in ("format", "hospital_name", "last_updated_on",
                                             "version", "affirmation", "rows_scanned")})
        rec["reason"] = NOTE if not meta.get("note") else f"{NOTE}; {meta['note']}"
        for (ct, code, setting), a in sorted(aggs.items()):
            charges.append(a.row(SID, ct, code, setting))
        rec["basket_items"] = len(charges)
        rec["status"] = "ok" if charges else "parsed_empty"
        if not charges:
            rec["reason"] += " parsed, but no CPT/HCPCS or MS-DRG codes were recognised"
            rec["gap_group"] = "blocked"
        print(f"{SID} done: {rec['format']}, {rec['rows_scanned']:,} rows, {len(charges):,} coded items")
    except Exception as e:  # noqa: BLE001
        rec["status"] = "failed"
        rec["reason"] = f"{NOTE}; {e}"
        rec["gap_group"] = mrf._classify_failure(str(e))
        print(f"{SID} FAILED: {e}")
    hospitals = {h["ccn"]: h for h in json.loads((OUT / "hospitals.json").read_text())}
    mrf.match_ccn(rec, hospitals)
    files = [f for f in json.loads((OUT / "mrf_files.json").read_text()) if f["seed_id"] != SID] + [rec]
    files.sort(key=lambda f: f["seed_id"])
    ch = [c for c in json.loads((OUT / "mrf_charges.json").read_text()) if c["seed_id"] != SID] + charges
    (OUT / "mrf_files.json").write_text(json.dumps(files, indent=1))
    (OUT / "mrf_charges.json").write_text(json.dumps(ch, indent=1))


if __name__ == "__main__":
    main()
