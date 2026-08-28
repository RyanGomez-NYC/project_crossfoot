"""
Merge all collector segments into one deduplicated dataset.

Segments overlap on purpose - several collectors were pointed at overlapping
universes so that coverage gaps would show up as gaps rather than as silence.
That means merging has to dedupe, and disagreements between two independent
reads of the same document are themselves a finding.

Dedupe runs in two passes:
  1. numeric identity  - (source_url, std_approved, std_denied, exp_approved)
     Two collectors reading the same row produce the same numbers.
  2. name identity     - (source_url, contract_id, normalized plan name)
     Catches records where counts were not published at all.
"""
from __future__ import annotations
import json, re
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

SEGMENTS = [
    "filings_2025.json",   # the hand-seeded first pass
    "seg_blues.json", "seg_national.json", "seg_regional.json",
    "seg_states.json", "seg_marketplace.json", "seg_sweep.json",
    "seg_uhc_ma.json", "seg_aetna_ahc.json", "seg_wave2.json", "seg_ca_ny.json",
    # wave 3 - territorial collectors, August 2026
    "seg_w3_tx_south.json", "seg_w3_southeast.json", "seg_w3_midwest.json",
    "seg_w3_plains.json", "seg_w3_west.json", "seg_w3_midatl.json",
    "seg_w3_newengland.json", "seg_w3_ky_nc_ms_ny.json", "seg_w3_blues.json",
    "seg_w3_ma_regional.json", "seg_w3_qhp.json",
    "seg_w3_gaps_a.json", "seg_w3_gaps_b.json",
    # wave 5 - machine extraction against the CMS reporting template, August 2026.
    # These come out of pipeline.preauth.extract rather than a collector: the
    # documents are fetched into data/raw/preauth/docs/ and parsed, so each row
    # carries the SHA-256 of the bytes it was read from.
    "seg_w5_humana.json", "seg_w5_cms_template.json", "seg_w5_states.json",
    "seg_w5_kaiser.json", "seg_w5_anthem.json", "seg_w5_metroplus.json",
    "seg_w5_blocked.json", "seg_w5_numbered.json", "seg_w5_browser.json",
    "seg_w5_bulleted.json", "seg_w5_dentaquest.json", "seg_w5_scorecard.json",
    "seg_w5_sectioned.json", "seg_w5_wellsense.json",
]

COVERAGE_CANON = {
    "medicare advantage": "Medicare Advantage",
    "medicaid managed care": "Medicaid Managed Care",
    "medicaid ffs": "Medicaid FFS",
    "marketplace qhp": "Marketplace QHP",
    "chip managed care": "CHIP Managed Care",
    "medicare-medicaid plan": "Medicare-Medicaid Plan",
}



ORG_ALIAS = {
    "centene corporation": "Centene", "centene / health net": "Centene",
    "centene / wellcare by health net": "Centene",
    "centene corporation / managed health services (mhs) indiana": "Centene",
    "centene corporation / home state health": "Centene",
    "centene corporation / meridian": "Centene",
    "calviva health (administered by health net)": "Centene",
    "community health plan of imperial valley (administered by health net)": "Centene",
    "aetna": "CVS Health / Aetna", "aetna / cvs health": "CVS Health / Aetna",
    "aetna (cvs health)": "CVS Health / Aetna", "cvs health (aetna)": "CVS Health / Aetna",
    "elevance health / anthem": "Elevance Health", "elevance health / wellpoint": "Elevance Health",
    "amerihealth caritas / performcare": "AmeriHealth Caritas",
    "capital district physicians' health plan (cdphp)": "CDPHP",
    "humana (independent care health plan / icare)": "Humana / iCare",
    "the cigna group": "Cigna", "sentara health plans": "Sentara Health",
    "univera healthcare (lifetime healthcare / excellus)": "Univera Healthcare",
    "excellus bluecross blueshield (lifetime healthcare)": "Excellus BlueCross BlueShield",
    "community first health plans, inc.": "Community First Health Plans",
    "community first health plans, inc. & community first insurance plans": "Community First Health Plans",
    "mississippi division of medicaid / truecare": "TrueCare",
    "magellan healthcare (wyoming care management entity)": "Magellan Healthcare",
    "commonwealth of massachusetts (published on mass.gov)": "Commonwealth of Massachusetts",
}


def canon_org(o):
    if not o:
        return o
    return ORG_ALIAS.get(o.strip().lower(), o.strip())


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", " ", s.lower())      # drop parenthetical qualifiers
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def norm_url(u: str | None) -> str:
    if not u:
        return ""
    return u.split("?")[0].split("#")[0].rstrip("/").lower()


def canon_coverage(c: str | None) -> str:
    if not c:
        return "Unclassified"
    return COVERAGE_CANON.get(c.strip().lower(), c.strip())


def completeness(r: dict) -> int:
    return sum(1 for v in r.values() if v not in (None, "", []))


def load_segments():
    records, failures = [], []
    for name in SEGMENTS:
        p = DATA / name
        if not p.exists():
            print(f"  ! missing {name}")
            continue
        blob = json.loads(p.read_text())
        rows = blob if isinstance(blob, list) else blob.get("filings", [])
        for r in rows:
            r["_segment"] = name.replace("seg_", "").replace(".json", "")
            r["coverage_type"] = canon_coverage(r.get("coverage_type"))
            r["parent_org"] = canon_org(r.get("parent_org"))
            records.append(r)
        if isinstance(blob, dict):
            for f in blob.get("failures", []):
                f["_segment"] = name.replace("seg_", "").replace(".json", "")
                failures.append(f)
        print(f"  {name:26} {len(rows):4} filings")
    return records, failures


def dedupe(records):
    conflicts = []

    # pass 1 - numeric identity
    by_num: dict[tuple, dict] = {}
    passthrough = []
    for r in records:
        key = (norm_url(r.get("source_url")), r.get("std_approved"),
               r.get("std_denied"), r.get("exp_approved"))
        if key[1] is None and key[2] is None:
            passthrough.append(r)
            continue
        prev = by_num.get(key)
        if prev is None or completeness(r) > completeness(prev):
            by_num[key] = r
    stage = list(by_num.values()) + passthrough

    # pass 2 - name identity, catches percentage-only records
    by_name: dict[tuple, dict] = {}
    for r in stage:
        key = (norm_url(r.get("source_url")), r.get("contract_id"),
               norm_name(r.get("plan_name")))
        prev = by_name.get(key)
        if prev is None:
            by_name[key] = r
            continue
        # Same document, same plan, two different reads.
        if (prev.get("std_approved"), prev.get("std_denied")) != \
           (r.get("std_approved"), r.get("std_denied")):
            conflicts.append({
                "plan_name": r.get("plan_name"),
                "source_url": r.get("source_url"),
                "read_a": f"{prev.get('std_approved')} approved / {prev.get('std_denied')} denied "
                          f"(segment {prev.get('_segment')})",
                "read_b": f"{r.get('std_approved')} approved / {r.get('std_denied')} denied "
                          f"(segment {r.get('_segment')})",
            })
        if completeness(r) > completeness(prev):
            by_name[key] = r

    return list(by_name.values()), conflicts


def main():
    print("loading segments:")
    records, failures = load_segments()
    print(f"  {'':26} {len(records):4} raw records\n")

    merged, conflicts = dedupe(records)
    merged.sort(key=lambda r: (r.get("parent_org") or "", r.get("plan_name") or ""))

    for i, r in enumerate(merged, 1):
        r.setdefault("filing_id", f"f{i:04d}")
        if not r.get("filing_id") or r["filing_id"].startswith("f0"):
            r["filing_id"] = f"f{i:04d}"

    (DATA / "merged_filings.json").write_text(json.dumps(merged, indent=1))
    (DATA / "merged_failures.json").write_text(json.dumps(failures, indent=1))
    (DATA / "merge_conflicts.json").write_text(json.dumps(conflicts, indent=1))

    print(f"merged   {len(merged)} unique filings "
          f"({len(records) - len(merged)} duplicates collapsed)")
    print(f"conflicts {len(conflicts)} same-document disagreements between collectors")
    print(f"failures  {len(failures)} documents that could not be read")
    orgs = len({r.get("parent_org") for r in merged})
    states = len({r.get("state") for r in merged if r.get("state")})
    print(f"coverage  {orgs} organizations, {states} states")


if __name__ == "__main__":
    main()
