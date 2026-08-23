#!/usr/bin/env python3
"""
Build the pricing dataset.

    python3 -m pipeline.prices.build --all              everything, ~20–60 min, mostly MRF downloads
    python3 -m pipeline.prices.build --central          the CMS / CHR / Urban / Census files only
    python3 -m pipeline.prices.build --mrf --mrf-limit 10   a quick pass over ten hospitals
    python3 -m pipeline.prices.build --rebuild          re-run normalize + validate from data/raw, no network

Writes everything to out/prices/ (git-ignored, the whole thing) and the
abridged public copies to data/ (committed). The MySQL dumps are produced from
out/prices/ by the web repository's tools/export_prices_sql.py.

Run from the repository root, on a machine with network access. Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import counties, enrich, fetch, medicare, mrf, sources, validate
from .basket import BASKET
from . import codes as codes_mod

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "prices"
PUBLIC = ROOT / "data"


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: list[dict], cols: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    cols = cols or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def read_json(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


# ---------------------------------------------------------------------------

def fetch_central(refresh: bool) -> dict[str, Path]:
    paths = {}
    for key, src in sources.SOURCES.items():
        url = sources.discover(src, log=log)
        log(f"fetching {key}: {url.split('/')[-1][:60]}")
        try:
            if key == "ahrq_systems" or src.candidates:
                last = None
                for cand in (src.candidates or sources.AHRQ_CANDIDATES):
                    try:
                        paths[key] = fetch.download(cand, src.local, refresh=refresh)
                        src.discovered_url = cand
                        log(f"  got {cand.split('/')[-1]}")
                        break
                    except fetch.FetchError as e:
                        last = e
                        log(f"  {cand.split('/')[-1]}: {e}")
                else:
                    raise last or fetch.FetchError("no candidate URL worked")
            else:
                paths[key] = fetch.download(url, src.local, refresh=refresh)
            rec = fetch.source_record(src.local) or {}
            log(f"  {rec.get('bytes', 0):>12,} bytes  sha256 {str(rec.get('sha256'))[:12]}…")
        except fetch.FetchError as e:
            if src.optional:
                log(f"  not fetched (optional): {e}")
            else:
                log(f"  FAILED: {e}")
    return paths


def load_central(paths: dict[str, Path]) -> dict:
    d: dict = {"hospitals": {}, "inpatient": [], "outpatient": [], "physician": [],
               "county": [], "urban_map": {}, "zcta": {}}
    if "inpatient" in paths:
        h1, d["inpatient"] = medicare.load_inpatient(paths["inpatient"], sources.SOURCES["inpatient"].data_year)
        log(f"inpatient: {len(d['inpatient']):,} rows, {len(h1):,} hospitals")
    else:
        h1 = {}
    if "outpatient" in paths:
        h2, d["outpatient"] = medicare.load_outpatient(paths["outpatient"], sources.SOURCES["outpatient"].data_year)
        log(f"outpatient: {len(d['outpatient']):,} rows, {len(h2):,} hospitals")
    else:
        h2 = {}
    d["hospitals"] = medicare.merge_hospitals(h1, h2)
    if "physician_geo" in paths:
        d["physician"] = medicare.load_physician_geo(paths["physician_geo"], sources.SOURCES["physician_geo"].data_year)
        log(f"physician (every code): {len(d['physician']):,} rows")
    chr_rows = counties.load_chr(paths["chr"]) if "chr" in paths else {}
    if chr_rows:
        log(f"county health rankings: {len(chr_rows):,} geographies")
    urban: dict = {}
    if "debt_county" in paths:
        urban, d["urban_map"] = counties.load_urban(paths["debt_county"], "county")
        log(f"debt in america (county): {len(urban):,} counties; columns used: {d['urban_map']}")
    for key, level in (("debt_state", "state"), ("debt_national", "nation")):
        if key in paths:
            try:
                extra, _ = counties.load_urban(paths[key], level)
                urban.update(extra)
                log(f"debt in america ({level}): {len(extra)} rows merged")
            except counties.SchemaError as e:
                log(f"  {key}: {e}")
    d["county"] = counties.build_county_profiles(chr_rows, urban)
    # --- enrichment: disease rates, age/poverty, health systems ---------------
    places_c, places_a, acs, ahrq = {}, {}, {}, {}
    if "places" in paths:
        try:
            places_c, places_a = enrich.load_places(paths["places"])
            log(f"cdc places: {len(places_c):,} counties")
        except enrich.SchemaError as e:
            log(f"  places: {e}")
    if "acs" in paths:
        try:
            acs = enrich.load_acs(paths["acs"])
            log(f"acs: {len(acs):,} counties")
        except enrich.SchemaError as e:
            log(f"  acs: {e}")
    enrich.attach_county_enrichment(d["county"], places_c, places_a, acs)
    if "ahrq_systems" in paths:
        try:
            ahrq, used = enrich.load_ahrq(paths["ahrq_systems"])
            n = enrich.attach_systems(d["hospitals"], ahrq)
            log(f"ahrq systems: {len(ahrq):,} linked hospitals in file; {n:,} of ours matched; columns {used}")
        except enrich.SchemaError as e:
            log(f"  ahrq: {e}")
    if not ahrq:
        enrich.attach_systems(d["hospitals"], {})   # keys present, all None
    d["ahrq"] = ahrq
    d["systems"] = enrich.build_systems(d["hospitals"], d["inpatient"], ahrq)
    log(f"health systems with Medicare rows: {len(d['systems']):,}")
    if "zcta_county" in paths:
        d["zcta"] = counties.load_zcta_county(paths["zcta_county"])
        n = counties.attach_county(d["hospitals"], d["zcta"])
        log(f"zip→county: {len(d['zcta']):,} ZCTAs; {n:,} of {len(d['hospitals']):,} hospitals placed in a county")
    return d


def run_mrf(hospitals: dict[str, dict], limit: int | None, only: list[str] | None,
            states: list[str] | None) -> tuple[list[dict], list[dict]]:
    seeds = mrf.load_seeds()
    if only:
        seeds = [s for s in seeds if s["seed_id"] in set(only)]
    if states:
        seeds = [s for s in seeds if s["state"] in set(states)]
    if limit:
        seeds = seeds[:limit]
    log(f"hospital price files: {len(seeds)} hospitals in the sample")
    files, charges = [], []
    for s in seeds:
        rec, rows = mrf.collect_one(s, log=log)
        mrf.match_ccn(rec, hospitals)
        files.append(rec)
        charges.extend(rows)
        if rec.get("ccn") and rec["ccn"] in hospitals:
            hospitals[rec["ccn"]]["mrf_status"] = rec["status"]
    return files, charges


# ---------------------------------------------------------------------------
# aggregates — computed in code from the normalized rows, and nothing else

def _wmean(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted mean over (value, weight) where both halves exist."""
    pairs = [(v, w) for v, w in pairs if v is not None and w]
    if not pairs:
        return None
    return round(sum(v * w for v, w in pairs) / sum(w for _, w in pairs), 3)


def _ratio_of_sums(pairs: list[tuple[float, float]]) -> float | None:
    """Σ numerator / Σ denominator over (num, den) pairs — "billed per $1 paid" for a group."""
    num = sum(n for n, _ in pairs)
    den = sum(d for _, d in pairs)
    return round(num / den, 3) if den else None


def _median(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def state_basket(d: dict, charges: list[dict], files: list[dict]) -> list[dict]:
    """
    One row per state per basket item, from whichever sources carry it:
      MS-DRG  Medicare average covered charge (discharge-weighted) and payment
      CPT     Medicare physician allowed amount (office / facility) and submitted charge
      both    the sampled hospital files' gross and cash prices (median across the sample)
    """
    seed_state = {f["seed_id"]: f["state"] for f in files}
    rows: dict[tuple[str, str, str], dict] = {}

    def row(state, ctype, code):
        k = (state, ctype, code)
        if k not in rows:
            b = next((x for x in BASKET if x["type"] == ctype and x["code"] == code), {})
            rows[k] = {"state": state, "code_type": ctype, "code": code, "label": b.get("label"),
                       "group": b.get("group"),
                       "medicare_charge": None, "medicare_payment": None, "medicare_n": None, "setting": None,
                       "mrf_gross_median": None, "mrf_cash_median": None, "mrf_negotiated_median": None,
                       "mrf_n": None}
        return rows[k]

    drg_codes = {b["code"] for b in BASKET if b["type"] == "MS-DRG"}
    by_state_drg: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in d["inpatient"]:
        if r["drg"] in drg_codes:
            st = d["hospitals"].get(r["ccn"], {}).get("state")
            if st:
                by_state_drg[(st, r["drg"])].append(r)
                by_state_drg[("US", r["drg"])].append(r)
    for (st, drg), rs in by_state_drg.items():
        x = row(st, "MS-DRG", drg)
        x["medicare_charge"] = _wmean([(r["avg_covered_charge"], r["discharges"]) for r in rs])
        x["medicare_payment"] = _wmean([(r["avg_total_payment"], r["discharges"]) for r in rs])
        x["medicare_n"] = sum(r["discharges"] or 0 for r in rs)

    # The physician file has an office row and a facility row for most codes.
    # The basket takes the setting where the service is actually performed:
    # the row with more services. An office visit (99213) is an office row by
    # a factor of 14; an emergency visit (99285) is a facility row by a factor
    # of 6,700; a CT head (70450) is facility by 20. An earlier rule forced
    # every 992xx code to the office row, which priced an ED visit off 1,357
    # office claims instead of 9 million facility ones.
    for r in d["physician"]:
        x = row(r["geo"], "CPT", r["hcpcs"])
        if x["medicare_n"] is None or (r["services"] or 0) > (x["medicare_n"] or 0):
            x["medicare_charge"] = r["avg_submitted_charge"]
            x["medicare_payment"] = r["avg_allowed"]
            x["medicare_n"] = r["services"]
            x["setting"] = "office" if r["place_of_service"] == "O" else "facility"

    by_state_item: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for c in charges:
        st = seed_state.get(c["seed_id"])
        if st:
            by_state_item[(st, c["code_type"], c["code"])].append(c)
            by_state_item[("US", c["code_type"], c["code"])].append(c)
    for (st, ct, code), cs in by_state_item.items():
        x = row(st, ct, code)
        x["mrf_gross_median"] = _median([c["gross"] for c in cs])
        x["mrf_cash_median"] = _median([c["cash"] for c in cs])
        x["mrf_negotiated_median"] = _median([c["negotiated_median"] for c in cs])
        x["mrf_n"] = len(cs)
    return sorted(rows.values(), key=lambda r: (r["state"] != "US", r["state"], r["code_type"], r["code"]))


def county_prices(d: dict) -> dict[str, dict]:
    """Per county: hospitals present, Medicare volume, and charge-to-payment ratios weighted by volume."""
    acc: dict[str, dict] = defaultdict(lambda: {"hospitals": set(), "inp": [], "out": [], "drg470": [], "discharges": 0})
    for r in d["inpatient"]:
        h = d["hospitals"].get(r["ccn"], {})
        fips = h.get("county_fips")
        if not fips:
            continue
        a = acc[fips]
        a["hospitals"].add(r["ccn"])
        a["discharges"] += r["discharges"] or 0
        # ratio of sums, not mean of ratios: the county "bills $X per $1 paid"
        # the same way the national figure and a health system's do
        if r["avg_covered_charge"] is not None and r["avg_total_payment"] is not None and r["discharges"]:
            a["inp"].append((r["avg_covered_charge"] * r["discharges"], r["avg_total_payment"] * r["discharges"]))
        if r["drg"] == "470":
            a["drg470"].append((r["avg_covered_charge"], r["discharges"]))
    for r in d["outpatient"]:
        h = d["hospitals"].get(r["ccn"], {})
        fips = h.get("county_fips")
        if not fips:
            continue
        a = acc[fips]
        a["hospitals"].add(r["ccn"])
        if r["avg_submitted_charge"] is not None and r["avg_allowed"] is not None and r["services"]:
            a["out"].append((r["avg_submitted_charge"] * r["services"], r["avg_allowed"] * r["services"]))
    out = {}
    for fips, a in acc.items():
        out[fips] = {
            "hospitals_n": len(a["hospitals"]),
            "inpatient_discharges": a["discharges"],
            "inpatient_charge_to_payment": _ratio_of_sums(a["inp"]),
            "outpatient_charge_to_allowed": _ratio_of_sums(a["out"]),
            "drg470_avg_charge": _wmean(a["drg470"]),
        }
    return out


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="central sources + the MRF sample")
    ap.add_argument("--central", action="store_true", help="fetch and load the central sources")
    ap.add_argument("--mrf", action="store_true", help="crawl the hospital price-file sample")
    ap.add_argument("--rebuild", action="store_true", help="no network: reload data/raw and out/prices/mrf_*.json")
    ap.add_argument("--refresh", action="store_true", help="re-download files already in data/raw")
    ap.add_argument("--mrf-limit", type=int, default=None)
    ap.add_argument("--mrf-only", nargs="*", help="seed ids, e.g. NY-01 OH-01")
    ap.add_argument("--mrf-states", nargs="*", help="two-letter state codes")
    args = ap.parse_args(argv)
    if not (args.all or args.central or args.mrf or args.rebuild):
        ap.print_help()
        return 2

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    do_central = args.all or args.central or args.rebuild
    do_mrf = args.all or args.mrf

    # --- central ---------------------------------------------------------
    if do_central:
        if args.rebuild:
            paths = {k: fetch.RAW_DIR / s.local for k, s in sources.SOURCES.items() if (fetch.RAW_DIR / s.local).exists()}
            log(f"rebuild: {len(paths)} raw files present")
        else:
            paths = fetch_central(args.refresh)
        d = load_central(paths)
    else:
        # an --mrf-only run still needs hospitals for the CCN match
        d = {"hospitals": read_json(OUT / "hospitals.json") or {}, "inpatient": [], "outpatient": [],
             "physician": [], "county": [], "urban_map": {}, "zcta": {}, "systems": read_json(OUT / "systems.json") or []}
        if isinstance(d["hospitals"], list):
            d["hospitals"] = {h["ccn"]: h for h in d["hospitals"]}
        if not d["hospitals"]:
            log("note: out/prices/hospitals.json not found; MRF rows will not be matched to a CCN")

    # --- MRF sample ------------------------------------------------------
    prev_files = read_json(OUT / "mrf_files.json") or []
    prev_charges = read_json(OUT / "mrf_charges.json") or []
    if do_mrf:
        files, charges = run_mrf(d["hospitals"], args.mrf_limit, args.mrf_only, args.mrf_states)
        if args.mrf_limit or args.mrf_only or args.mrf_states:
            # a partial run replaces only the hospitals it touched
            touched = {f["seed_id"] for f in files}
            files = [f for f in prev_files if f["seed_id"] not in touched] + files
            charges = [c for c in prev_charges if c["seed_id"] not in touched] + charges
    else:
        files, charges = prev_files, prev_charges
        for f in files:
            if f.get("ccn") and f["ccn"] in d["hospitals"]:
                d["hospitals"][f["ccn"]]["mrf_status"] = f.get("status")

    # --- validate ---------------------------------------------------------
    findings = validate.run(charges, files, d["inpatient"], d["outpatient"], d["physician"])
    by_sev = defaultdict(int)
    for f in findings:
        by_sev[f["severity"]] += 1
    log(f"\nfindings: {len(findings):,} ({by_sev['error']} error, {by_sev['warn']} warn, {by_sev['info']} info)")

    # --- aggregates -------------------------------------------------------
    # state_basket() sees every code the sources carry. The curated basket is
    # the 48 items in basket.py; everything else is the code catalog. Two
    # outputs, two tables, two jobs — the site's comparisons run on the basket,
    # the lookup runs on the catalog.
    all_rows = state_basket(d, charges, files)
    basket_keys = {(b["type"], b["code"]) for b in BASKET}
    basket_rows = [r for r in all_rows if (r["code_type"], r["code"]) in basket_keys]
    catalog_rows = [r for r in all_rows if (r["code_type"], r["code"]) not in basket_keys]

    # the code dictionary: what is a code, and where its definition came from
    hcpcs_list = codes_mod.load_hcpcs_release(fetch.RAW_DIR / sources.SOURCES["hcpcs"].local)
    msdrg_list = codes_mod.load_msdrg_table(fetch.RAW_DIR / sources.SOURCES["msdrg"].local)
    code_rows = codes_mod.build(d["physician"], d["inpatient"], charges, BASKET, hcpcs_list, msdrg_list)
    code_summary = codes_mod.summary(code_rows)
    log(f"codes: {code_summary['codes']:,} seen; official CPT/HCPCS {code_summary['official_cpt']:,}, "
        f"official MS-DRG {code_summary['official_drg']:,}; HCPCS release {len(hcpcs_list):,} codes, "
        f"MS-DRG table {len(msdrg_list):,}")
    # the catalog only carries codes that survive the dictionary: unverified
    # strings stay in the dictionary (so the miss is published) but get no
    # state rows, because a price for a non-code is not a price
    keep = {(c["code_type"], c["code"]) for c in code_rows if c["status"] != codes_mod.STATUS_UNVERIFIED}
    dropped = sum(1 for r in catalog_rows if (r["code_type"], r["code"]) not in keep)
    catalog_rows = [r for r in catalog_rows if (r["code_type"], r["code"]) in keep]
    log(f"catalog: {len(catalog_rows):,} state rows kept, {dropped:,} dropped as unverified codes")
    cp = county_prices(d)
    for c in d["county"]:
        c.update(cp.get(c["fips"], {"hospitals_n": 0, "inpatient_discharges": None,
                                    "inpatient_charge_to_payment": None,
                                    "outpatient_charge_to_allowed": None, "drg470_avg_charge": None}))

    # --- write the full dataset -------------------------------------------
    hospitals = sorted(d["hospitals"].values(), key=lambda h: h["ccn"])
    if do_central:
        write_json(OUT / "hospitals.json", hospitals)
        write_csv(OUT / "hospitals.csv", hospitals)
        write_csv(OUT / "inpatient.csv", d["inpatient"])
        write_csv(OUT / "outpatient.csv", d["outpatient"])
        write_csv(OUT / "physician_geo.csv", d["physician"])
        write_csv(OUT / "county_profile.csv", d["county"])
        write_json(OUT / "county_profile.json", d["county"])
        write_csv(OUT / "systems.csv", d["systems"])
        write_json(OUT / "systems.json", d["systems"])
    write_json(OUT / "mrf_files.json", files)
    write_csv(OUT / "mrf_files.csv", files)
    write_json(OUT / "mrf_charges.json", charges)
    write_csv(OUT / "mrf_charges.csv", charges)
    write_csv(OUT / "findings.csv", findings)
    write_json(OUT / "findings.json", findings)
    write_csv(OUT / "state_basket.csv", basket_rows)
    write_json(OUT / "state_basket.json", basket_rows)
    write_csv(OUT / "code_state.csv", catalog_rows)
    write_json(OUT / "code_state.json", catalog_rows)
    write_csv(OUT / "codes.csv", code_rows)
    write_json(OUT / "codes.json", code_rows)

    status = defaultdict(int)
    for f in files:
        status[f.get("status") or "?"] += 1
    gap_groups = defaultdict(int)
    for f in files:
        if f.get("gap_group"):
            gap_groups[f["gap_group"]] += 1

    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": {k: {"title": s.title, "publisher": s.publisher, "url": s.discovered_url or s.url,
                        "pinned_url": s.url, "data_year": s.data_year, "license": s.license,
                        "landing": s.landing, "notes": s.notes, "fetched": fetch.source_record(s.local)}
                    for k, s in sources.SOURCES.items()},
        "urban_columns_used": d.get("urban_map", {}),
        "basket": BASKET,
        "counts": {
            "hospitals": len(hospitals),
            "hospitals_with_county": sum(1 for h in hospitals if h.get("county_fips")),
            "hospitals_with_system": sum(1 for h in hospitals if h.get("system_id")),
            "systems": len(d.get("systems", [])),
            "counties_with_places": sum(1 for c in d["county"] if c.get("obesity_pct") is not None),
            "counties_with_acs": sum(1 for c in d["county"] if c.get("median_age") is not None),
            "inpatient_rows": len(d["inpatient"]),
            "outpatient_rows": len(d["outpatient"]),
            "physician_rows": len(d["physician"]),
            "counties": sum(1 for c in d["county"] if c["level"] == "county"),
            "counties_with_debt": sum(1 for c in d["county"] if c["level"] == "county" and c.get("medical_debt_pct") is not None),
            "mrf_sample": len(files),
            "mrf_status": dict(status),
            "mrf_gap_groups": dict(gap_groups),
            "mrf_basket_rows": len(charges),
            "basket_state_rows": len(basket_rows),
            "catalog_state_rows": len(catalog_rows),
            "codes": code_summary,
            "findings": dict(by_sev),
        },
        "notes": [
            "Every rate and ratio is computed from the published counts in the same row.",
            "NULL means the publisher printed nothing. It never means zero.",
            "Medical debt is debt in collections on credit records (Urban Institute). "
            "No public dataset records medical bankruptcies; none is claimed here.",
            "The hospital price-file sample is a sample. Its findings describe the hospitals "
            "sampled, not the 6,000 that were not.",
        ],
    }
    write_json(OUT / "manifest.json", manifest)

    # --- abridged public copies ------------------------------------------
    write_csv(PUBLIC / "prices_state_basket.csv", basket_rows)
    write_csv(PUBLIC / "prices_codes.csv", code_rows)
    write_csv(PUBLIC / "prices_mrf_sample.csv", charges)
    write_csv(PUBLIC / "prices_mrf_files.csv", files,
              ["seed_id", "state", "hospital", "domain", "ccn", "ccn_match", "hpt_url", "hpt_status",
               "locations_listed", "mrf_url", "format", "hospital_name", "last_updated_on", "version",
               "bytes", "sha256", "rows_scanned", "basket_items", "status", "gap_group", "reason", "fetched_at"])
    write_csv(PUBLIC / "prices_findings.csv", [f for f in findings if f["scope"] == "mrf" or f["severity"] == "error"])
    if do_central:
        write_csv(PUBLIC / "prices_county_profile.csv", [c for c in d["county"]])
    write_json(PUBLIC / "prices_manifest.json", manifest)

    log(f"\nout/prices/ written; abridged copies in data/. {time.time() - t0:.0f}s")
    log(json.dumps(manifest["counts"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
