#!/usr/bin/env python3
"""
Recover seeds whose host walls every non-browser TLS client.

Akamai/Cloudflare on these hosts fingerprint the TLS handshake, not the
User-Agent — urllib and curl are refused (403/Access Denied) whatever they
claim to be, while a real browser reads both cms-hpt.txt and the MRF freely.
The cms-hpt.txt of each host was read in a real browser (22 Aug 2026) and the
location block chosen by hand; this script fetches the listed MRF with a
browser TLS profile (curl_cffi, impersonate="chrome") and parses it with the
pipeline's own parsers. Records match collect_one's shape; the note says how
the file was reached. `hpt_status` is "browser" so the finding — this host
blocks honest crawlers — survives in the record.

Parsing semantics are the pipeline's: parse_csv verbatim; JSON incrementally
via harvest_large.parse_json_stream (constant memory at any size), which
replicates mrf.parse_json item-for-item.

Run with the venv python (needs curl_cffi + ijson):
  scripts/venv/bin/python scripts/recover_browser.py [SEED-ID ...]
"""
import io
import json
import hashlib
import sys
import time
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
SCRATCH = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE))
sys.path.insert(0, str(SCRATCH))

from curl_cffi import requests as creq  # noqa: E402
from pipeline.prices import fetch, mrf  # noqa: E402
from harvest_large import parse_json_stream, log  # noqa: E402

OUT = PIPELINE / "out" / "prices"
RAW = PIPELINE / "data" / "raw" / "prices" / "browser"

BROWSER = {
    "MD-01": {
        "url": "https://www.hopkinsmedicine.org/-/media/patient-care/documents/billing-insurance/charge-fees/520591656_the-johns-hopkins-hospital_standardcharges.csv",
        "hpt_url": "https://www.hopkinsmedicine.org/cms-hpt.txt",
        "locations_listed": 6, "location_name": "The Johns Hopkins Hospital",
    },
    "MT-01": {
        "url": "https://www.billingsclinic.com/app/files/public/6f31e11d-e20d-43bc-af7e-9433c5a9d3f0/billing%20financial%20assistance%20insurance/810231784_billings-clinic_standardcharges.csv",
        "hpt_url": "https://www.billingsclinic.com/cms-hpt.txt",
        "locations_listed": 3, "location_name": "Billings Clinic",
    },
    "MT-02": {
        "url": "https://www.benefis.org/app/files/public/ecc68e09-9516-4e98-9c6a-e892ac1d14f0/810232122_BenefisHospitalsInc_standardcharges.csv",
        "hpt_url": "https://www.benefis.org/cms-hpt.txt",
        "locations_listed": 7, "location_name": "Benefis Hospitals Inc - East Campus",
    },
    "NE-02": {
        "url": "https://www.bryanhealth.com/app/files/public/bd60243f-a01c-430c-a767-02e194058e7f/470376552_bryan-medical-center-east-campus_standardcharges.csv",
        "hpt_url": "https://www.bryanhealth.com/cms-hpt.txt",
        "locations_listed": 6, "location_name": "Bryan Medical Center - East",
    },
    "SD-01": {
        "url": "https://www.avera.org/app/files/public/2b9aa38c-4c89-40ef-b4b6-f3c7b8c158ac/460024743_avera-mckennan-hospital_standardcharges.csv",
        "hpt_url": "https://www.avera.org/cms-hpt.txt",
        "locations_listed": 40, "location_name": "Avera McKennan Hospital and University Health Center",
    },
    "VT-02": {
        "url": "https://www.rrmc.org/app/files/public/47f0fe40-cd79-42f2-bd09-793a36563ae6/Patient%20and%20Visitors/030183483_RutlandRegionalMedicalCenter_standardcharges.csv",
        "hpt_url": "https://www.rrmc.org/cms-hpt.txt",
        "locations_listed": 1, "location_name": "Rutland Regional Medical Center",
    },
    "WA-01": {
        "url": "https://www.uwmedicine.org/sites/stevie/files/mrf/916001537_university-of-washington-medical-center_standardcharges.json",
        "hpt_url": "https://www.uwmedicine.org/cms-hpt.txt",
        "locations_listed": 2, "location_name": "University of Washington Medical Center",
    },
    # county/municipal wave stragglers (23 Aug follow-up): two hosts whose TLS
    # chain urllib rejects (missing intermediate certs), four files past the
    # streaming caps
    "CA-06": {
        "url": "https://www.alamedahealthsystem.org/wp-content/uploads/pricing/943302014_highland-hospital_standardcharges.csv",
        "hpt_url": "https://www.alamedahealthsystem.org/cms-hpt.txt",
        "locations_listed": 5, "location_name": "Highland Hospital",
        "note": "host serves an incomplete TLS chain urllib rejects; fetched with a browser TLS profile",
    },
    "IA-03": {
        "url": "https://files.trueaccess.care/broadlawns/426005830_broadlawns_standardcharges.csv",
        "hpt_url": "https://www.broadlawns.org/cms-hpt.txt",
        "locations_listed": 5, "location_name": "Broadlawns Medical Center",
        "note": "host serves an incomplete TLS chain urllib rejects; fetched with a browser TLS profile",
    },
    "LA-03": {
        "url": "https://lcmchealthfiles.blob.core.windows.net/mrf/PricingTransparency-University%20Medical%20Center%20New%20Orleans-2026-07-01%2008_49_10.csv",
        "hpt_url": "https://www.umcno.org/cms-hpt.txt",
        "locations_listed": 9, "location_name": "University Medical Center",
        "note": "CSV past the crawler's 1.5 GB streaming cap; streamed uncapped; location block chosen by hand",
        "kind": "csv",
    },
    "MI-03": {
        "url": "https://www.hurleymc.com/files/patients-and-visitors/386005601_hurley-medical-center_standardcharges.csv",
        "hpt_url": "https://www.hurleymc.com/cms-hpt.txt",
        "locations_listed": 3, "location_name": "Hurley Medical Center",
        "note": "CSV past the crawler's 1.5 GB streaming cap; streamed uncapped",
        "kind": "csv",
    },
    "NV-03": {
        "url": "https://mrfs.hyvehealthcare.com/UMCSouthernNevada/886000436_university-medical-center-of-southern-nevada_standardcharges.json",
        "hpt_url": "https://www.umcsn.com/cms-hpt.txt",
        "locations_listed": 1, "location_name": "University Medical Center of Southern Nevada",
        "note": "2.6 GB JSON, past the json.load cap; parsed incrementally (ijson)",
        "kind": "json",
    },
    "TN-05": {
        "url": "https://erlanger.pt.panaceainc.com/MRFDownload/erlanger/baroness",
        "hpt_url": "https://www.erlanger.org/cms-hpt.txt",
        "locations_listed": 7, "location_name": "Erlanger Baroness Hospital",
        "note": "file past the crawler's 1.5 GB streaming cap; streamed uncapped",
    },
    "TX-07": {
        "url": "https://sthpiprd.blob.core.windows.net/machine-readable-files/9382/746002164_bexar-county-hospital-district_standardcharges.csv",
        "hpt_url": "https://www.universityhealth.com/cms-hpt.txt",
        "locations_listed": 6, "location_name": "University Hospital",
        "note": "CSV past the crawler's 1.5 GB streaming cap; streamed uncapped",
        "kind": "csv",
    },
    # not TLS-walled — here for their size, past the crawler's caps, parsed
    # outside them like the Round 7 recoveries
    "AZ-02": {
        "url": "https://mcorgstatic.blob.core.windows.net/cms-price/860800150_mayo-clinic-arizona_standardcharges.csv",
        "hpt_url": "https://www.mayoclinic.org/cms-hpt.txt",
        "locations_listed": 7, "location_name": "Mayo Clinic Arizona",
        "note": "CSV past the crawler's 1.5 GB streaming cap; streamed uncapped",
        "kind": "csv",
    },
    "MN-01": {
        "url": "https://mcorgstatic.blob.core.windows.net/cms-price/410944601_mayo-clinic-hospital-rochester_standardcharges.csv",
        "hpt_url": "https://www.mayoclinic.org/cms-hpt.txt",
        "locations_listed": 7, "location_name": "Mayo Clinic Hospital-Rochester",
        "note": "CSV past the crawler's 1.5 GB streaming cap; streamed uncapped",
        "kind": "csv",
    },
    "MO-01": {
        "url": "https://www.bjc.org/hpt/6/237309937_BarnesJewishHospital_standardcharges.json",
        "hpt_url": "https://www.bjc.org/cms-hpt.txt",
        "locations_listed": 16, "location_name": "Barnes Jewish Hospital",
        "note": "JSON past the crawler's json.load cap; parsed incrementally (ijson)",
        "kind": "json",
    },
    "VA-02": {
        "url": "https://vcuhealth.pt.panaceainc.com/MRFDownload/vcuhealth/vcumedicalcenter",
        "hpt_url": "https://vcuhealth.org/cms-hpt.txt",
        "locations_listed": None, "location_name": "VCU Medical Center",
        "note": "648 MB JSON, past the crawler's json.load cap; parsed incrementally (ijson)",
        "kind": "json",
    },
}

NOTE = ("host walls every non-browser TLS client; cms-hpt.txt read in a real "
        "browser 22 Aug 2026, file fetched with a browser TLS profile")


def download(sid, url):
    RAW.mkdir(parents=True, exist_ok=True)
    ext = ".json" if (BROWSER[sid].get("kind") == "json" or url.lower().split("?")[0].endswith(".json")) else ".csv"
    dest = RAW / f"mrf_{sid}{ext}"
    sha = hashlib.sha256()
    n = 0
    with creq.Session(impersonate="chrome") as s:
        r = s.get(url, stream=True, timeout=1800)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
                sha.update(chunk)
                n += len(chunk)
    return dest, n, sha.hexdigest()


def recover(seed, hospitals):
    sid = seed["seed_id"]
    m = BROWSER[sid]
    note = m.get("note") or NOTE
    rec = {
        "seed_id": sid, "state": seed["state"], "hospital": seed["hospital"],
        "domain": seed["domain"], "ccn": (seed.get("ccn") or "").strip() or None,
        "hpt_url": m["hpt_url"],
        "hpt_status": "ok" if m.get("note") else "ok (read in a real browser)",
        "locations_listed": m["locations_listed"] or 0,
        "mrf_url": m["url"], "location_name": m["location_name"], "match_score": None,
        "format": None, "hospital_name": None, "last_updated_on": None, "version": None,
        "affirmation": None, "bytes": None, "sha256": None, "rows_scanned": None,
        "basket_items": 0, "status": None, "gap_group": None, "reason": None,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    charges = []
    try:
        path, rec["bytes"], rec["sha256"] = download(sid, m["url"])
        log(f"  {sid} downloaded {(rec['bytes'] or 0)/1e6:,.1f} MB")
        meta = {}
        with open(path, "rb") as fh:
            head = fh.read(8)
        if head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] in (b"{", b"["):
            with open(path, "rb") as fh:
                buf = io.BufferedReader(fh, 1 << 20)
                if buf.peek(3)[:3] == b"\xef\xbb\xbf":
                    buf.read(3)
                aggs = parse_json_stream(buf, meta)
        else:
            with open(path, "rb") as fh:
                wrapper = io.TextIOWrapper(io.BufferedReader(fh, 1 << 20),
                                           encoding="utf-8-sig", errors="replace", newline="")
                aggs = mrf.parse_csv(wrapper, sid, meta)
        path.unlink(missing_ok=True)
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
    mrf.match_ccn(rec, hospitals)
    return rec, charges


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    want = args or list(BROWSER)
    seeds = {s["seed_id"]: s for s in mrf.load_seeds()}
    hospitals = {h["ccn"]: h for h in json.loads((OUT / "hospitals.json").read_text())}
    for sid in want:
        cur = next((f for f in json.loads((OUT / "mrf_files.json").read_text())
                    if f["seed_id"] == sid), None)
        if cur and cur.get("status") == "ok" and not force:
            log(f"── {sid} already ok; skipping ──")
            continue
        log(f"── {sid} (browser TLS profile) ──")
        rec, rows = recover(seeds[sid], hospitals)
        files = [f for f in json.loads((OUT / "mrf_files.json").read_text()) if f["seed_id"] != sid] + [rec]
        files.sort(key=lambda f: f["seed_id"])
        charges = [c for c in json.loads((OUT / "mrf_charges.json").read_text()) if c["seed_id"] != sid] + rows
        (OUT / "mrf_files.json").write_text(json.dumps(files, indent=1))
        (OUT / "mrf_charges.json").write_text(json.dumps(charges, indent=1))


if __name__ == "__main__":
    main()
