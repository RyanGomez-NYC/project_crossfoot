"""
Join what we collected to what the rule requires -- the coverage ledger.

Every filing in data/seg_*.json is matched to a row of data/seeds/pa_entities.csv
and every entity is then either covered or not. The output is not a prettier way
of counting filings; it is the answer to a different question: *which obliged
payers have published, and which have not.* That question needs a denominator,
which is why universe.py had to come first.

Matching is deliberately conservative and every match records how it was made:

    contract   an exact CMS contract ID. Unambiguous; the only automatic match
               allowed to stand without a name agreeing.
    name       state agrees and the normalized plan names agree closely.
    manual     an entry in ALIASES, written by hand after looking at both.

Anything below the threshold is left unmatched rather than guessed. An unmatched
filing is a real signal -- usually a plan we captured whose entity we have not
placed, sometimes a payer publishing under a brand CMS does not use -- and
burying it inside a fuzzy match would hide it.

    python3 -m pipeline.preauth.match
    python3 -m pipeline.preauth.match --targets 200   # write the work queue

Outputs:
    out/preauth/coverage_ledger.csv   one row per obliged entity, covered or not
    out/preauth/unmatched_filings.csv filings we hold that no entity claimed
    out/preauth/targets.csv           the highest-value uncovered entities, in order
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = ROOT / "out" / "preauth"

# Words that carry no identity: every third Medicaid plan is a "health plan of"
# something. Stripping them before comparison is what makes "Superior HealthPlan"
# and "Superior Health Plan, Inc." the same plan and keeps "Superior" and
# "Sunflower" different ones.
NOISE = re.compile(
    r"\b(inc|llc|lp|corp|corporation|company|co|the|a|of|and|health|healthcare|"
    r"health\s?plan|healthplan|plan|plans|insurance|ins|medical|medicaid|medicare|"
    r"advantage|group|systems?|services?|network|choice|community|care|"
    r"llc\.|inc\.|hmo|ppo|snp|dsnp|hmo\-pos|pos)\b", re.I)

SEGMENT_OF = {
    "Medicare Advantage": "Medicare Advantage",
    "Medicaid Managed Care": "Medicaid Managed Care",
    "Marketplace QHP": "Marketplace QHP",
    "Medicaid FFS": "Medicaid FFS",
    "CHIP Managed Care": "CHIP Managed Care",
    "CHIP FFS": "CHIP FFS",
    "Medicare-Medicaid Plan": "Medicare Advantage",   # MMPs report on the MA side
}

# Hand-checked matches the automatic rules will not make, because the payer's
# published brand and its CMS name share no words. Each one was read off the
# source document.
# Each entry was written after reading the filing and the entity side by side.
# The reasons vary -- a rebrand CMS has not caught up with (Wellpoint was
# Amerigroup in DC and Highmark's WV Medicaid plan in the 2024 table), a brand
# the table names differently (EmblemHealth files under HIP), a state suffix the
# names disagree on -- and none of them are safe to automate.
ALIASES: dict[tuple, str] = {
    ("OR", "share oregon"): "mcd:OR:healthshare-of-oregon",
    ("WA", "coordinated apple"): "mcd:WA:coordinated-care-of-washington",
    ("MI", "macomb county mental"): "mcd:MI:macomb-county-cmh-services",
    ("DC", "wellpoint district columbia"): "mcd:DC:amerigroup-district-of-columbia",
    ("WV", "highmark west virginia"): "mcd:WV:wellpoint-west-virginia",
    ("NY", "emblemhealth harp combined"): "mcd:NY:hip-combined",
    # The DC Dual Choice rows are Medicare-Medicaid Plans, which the segment map
    # sends to the MA pool -- but DC's only UHC entity is the Medicaid plan, and
    # an MMP discharges the Medicaid side's obligation.
    ("DC", "district columbia dual program"): "mcd:DC:unitedhealthcare-community-plan-of-district-of-columbia",
    ("DC", "district columbia dual ltss"): "mcd:DC:unitedhealthcare-community-plan-of-district-of-columbia",
    ("VA", "virginia cardinal"): "mcd:VA:unitedhealthcare",
}

NAME_THRESHOLD = 0.86

# The brands that identify a parent's plans on sight. A filing whose parent
# carries one of these tokens may be matched to an entity whose name carries the
# same token -- but only when that entity is the ONLY candidate in its state and
# segment. One candidate is an identification; two is a guess, and guesses are
# what the unmatched file is for.
BRANDS = {
    "oscar": ["oscar"],
    "devoted": ["devoted"],
    "scan": ["scan health"],
    "cigna": ["cigna"],
    "caresource": ["caresource"],
    "ambetter": ["ambetter"],
    "selecthealth": ["selecthealth"],
    "bcbsma": ["blue cross blue shield of massachusetts"],
    "unitedhealthcare": ["unitedhealth", "united healthcare", "unitedhealthcare"],
    "molina": ["molina"],
    "kaiser": ["kaiser"],
    "humana": ["humana"],
    "aetna": ["aetna"],
    "centene": ["centene", "ambetter", "wellcare"],
    "anthem": ["anthem", "elevance"],
    "amerihealth": ["amerihealth"],
    "highmark": ["highmark"],
    "wellpoint": ["wellpoint"],
}


def brand_of(text: str) -> str | None:
    low = (text or "").lower()
    for brand, tokens in BRANDS.items():
        if any(t in low for t in tokens):
            return brand
    return None


def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = NOISE.sub(" ", s)
    return " ".join(s.split())


def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # token containment beats character ratio for "superior" vs "superior texas"
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def load_entities() -> list[dict]:
    with open(DATA / "seeds" / "pa_entities.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_filings() -> list[dict]:
    rows = []
    for p in sorted(DATA.glob("seg_*.json")) + [DATA / "filings_2025.json"]:
        if not p.exists():
            continue
        blob = json.loads(p.read_text())
        recs = blob.get("filings", []) if isinstance(blob, dict) else blob
        for r in recs:
            r["_segment_file"] = p.name
            rows.append(r)
    return rows


def match(filings: list[dict], entities: list[dict]) -> tuple[dict, list[dict]]:
    by_contract = {e["entity_key"]: e for e in entities if e["filing_unit"] == "ma_contract"}
    by_state: dict[str, list[dict]] = defaultdict(list)
    for e in entities:
        by_state[e["state"]].append(e)
    for e in entities:
        e["_norm"] = norm(e["entity_name"])

    by_ffs = {e["entity_key"]: e for e in entities if e["filing_unit"] == "ffs_state"
              and e["segment"] == "Medicaid FFS"}
    by_chipffs = {e["entity_key"]: e for e in entities if e["filing_unit"] == "ffs_state"
                  and e["segment"] == "CHIP FFS"}
    # The rule reaches QHP issuers on the FEDERALLY facilitated exchanges only.
    # Which states those are is not a list to maintain by hand -- it is exactly
    # the set of states in the federal plan-attributes PUF. A QHP filing from a
    # state-based marketplace is real data we collected, and correctly outside
    # this denominator; it is reported as out_of_universe, not as a failure.
    ffe_states = {e["state"] for e in entities if e["filing_unit"] == "qhp_issuer"}

    covered: dict[str, list[dict]] = defaultdict(list)
    unmatched: list[dict] = []
    out_of_universe: list[dict] = []

    for f in filings:
        seg = SEGMENT_OF.get(f.get("coverage_type") or "", "")
        state = (f.get("state") or "").strip()
        cid = (f.get("contract_id") or "").strip()
        if not cid:
            # Plenty of collectors put the contract ID in the plan name
            # ("UnitedHealthcare Medicare Advantage - Contract H0294") and left
            # the field blank. The ID is unambiguous wherever it appears.
            m = re.search(r"\b([HR]\d{4})\b", f.get("plan_name") or "")
            if m:
                cid = m.group(1)
        hit = how = None

        # Fee-for-service is a state programme; there is one per state and the
        # payer's own name for it is irrelevant.
        if f.get("coverage_type") == "Medicaid FFS" and state in by_ffs:
            hit, how = by_ffs[state], "state"
            # Some states report Medicaid and CHIP fee-for-service in one
            # document and say so -- California's report names CHIP FFS in its
            # own scope, Colorado's names CHP+, New Jersey files under the
            # FamilyCare umbrella that includes CHIP. Where the document itself
            # carries that evidence, the one filing discharges both of the
            # state's obligations; where it does not, the CHIP entity stays
            # uncovered rather than being presumed included.
            ev = ((f.get("plan_name") or "") + " " + (f.get("extraction_note") or "")).lower()
            if re.search(r"\bchip\b|chp\+|familycare", ev) and state in by_chipffs:
                f2 = dict(f)
                f2["_how"] = "state"
                covered[by_chipffs[state]["entity_id"]].append(f2)
        elif f.get("coverage_type") == "CHIP FFS" and state in by_chipffs:
            hit, how = by_chipffs[state], "state"
        elif f.get("coverage_type") == "Marketplace QHP" and state and state not in ffe_states:
            f["_why_out"] = "state-based marketplace: outside the FFE universe the rule covers"
            out_of_universe.append(f)
            continue
        elif f.get("coverage_type") in ("CHIP Managed Care",):
            f["_why_out"] = "CHIP managed care universe not yet built (no CMS plan-level file)"
            out_of_universe.append(f)
            continue

        # A QHP filing that prints its issuer's five-digit HIOS ID -- "Ambetter
        # from Superior HealthPlan (29418)" -- identifies the issuer exactly,
        # the same way a contract ID identifies an MA contract.
        if hit is None and f.get("coverage_type") == "Marketplace QHP":
            hm = re.search(r"\b(\d{5})\b", f.get("plan_name") or "")
            if hm:
                cand = [e for e in entities if e["filing_unit"] == "qhp_issuer"
                        and e["entity_key"] == hm.group(1)]
                if len(cand) == 1:
                    hit, how = cand[0], "hios"

        if hit is None and cid and cid in by_contract:
            hit, how = by_contract[cid], "contract"
        elif hit is None:
            key = (state, norm(f.get("plan_name", "")))
            if key in ALIASES:
                hit = next((e for e in entities if e["entity_id"] == ALIASES[key]), None)
                how = "manual"
            if hit is None and state:
                n = norm(f.get("plan_name", ""))
                pool = [e for e in by_state[state]
                        if not seg or e["segment"] == seg or seg == "Medicare Advantage"]
                best, score = None, 0.0
                for e in pool:
                    s = similar(n, e["_norm"])
                    if s > score:
                        best, score = e, s
                if best is not None and score >= NAME_THRESHOLD:
                    hit, how = best, "name"
                else:
                    # Second try: the payer's published brand often names the
                    # parent ("BCBSNE Medicare Advantage HMO") where CMS names
                    # the subsidiary. Compare against the parent field too, and
                    # only accept when the segment also agrees -- a parent runs
                    # several plans in a state and the segment is what separates
                    # them.
                    pn = norm(f.get("parent_org", ""))
                    cand = [e for e in pool if e["segment"] == seg] if seg else []
                    best2, score2 = None, 0.0
                    for e in cand:
                        s2 = max(similar(pn, e["_norm"]), similar(pn, norm(e["parent_org"])))
                        if s2 > score2:
                            best2, score2 = e, s2
                    if best2 is not None and score2 >= NAME_THRESHOLD and len(cand) > 0:
                        hit, how = best2, "parent"
                    else:
                        # Brand pass: the filing names a program ("Arizona AHCCCS
                        # Complete Care") and its parent names the brand. When
                        # exactly one entity in this state and segment carries
                        # the same brand, that is an identification.
                        fb = brand_of(f.get("parent_org", "")) or brand_of(f.get("plan_name", ""))
                        if fb and seg:
                            branded = [e for e in pool if e["segment"] == seg
                                       and (brand_of(e["entity_name"]) == fb
                                            or brand_of(e["parent_org"]) == fb)]
                            if len(branded) == 1:
                                hit, how = branded[0], "brand"

        if hit is None:
            # Aggregate-organization filings. Some payers publish one document
            # for the whole organization where the rule sets contract- or
            # issuer-level reporting -- Devoted for its 33 MA contracts, SCAN
            # for its ten, Cigna one combined figure for every FFE issuer. The
            # document is the organization discharging (imperfectly) every one
            # of those obligations at once, so every one of the brand's in-scope
            # entities in the segment is covered by it -- and match_how says
            # 'aggregate' so nobody mistakes the coverage for per-entity data.
            # It only fires for a filing with no contract, no state and a
            # recognized brand: anything narrower identifies a single entity
            # and is handled above.
            # Either name may be absent -- a plan that files on its state's
            # website carries no parent, and get() returning a stored None is
            # not the same as returning the default.
            fb = brand_of((f.get("parent_org") or "") + " " + (f.get("plan_name") or ""))
            if fb and not cid:
                seg2 = SEGMENT_OF.get(f.get("coverage_type") or "", "")
                pool2 = [e for e in entities if e["segment"] == seg2
                         and (brand_of(e["entity_name"]) == fb
                              or brand_of(e["parent_org"]) == fb)
                         and e["in_scope_cy2025"] == "yes"
                         and (not state or e["state"] == state
                              or state in (e["states_served"] or ""))]
                # One org filing standing for every contract the brand holds in
                # its scope. A single candidate is an identification; several
                # are covered as an aggregate and labelled so.
                if len(pool2) == 1:
                    hit, how = pool2[0], "brand"
                if hit is None and len(pool2) >= 2:
                    for e in pool2:
                        f2 = dict(f)
                        f2["_how"] = "aggregate"
                        covered[e["entity_id"]].append(f2)
                    continue
        if hit is None:
            unmatched.append(f)
        else:
            f["_how"] = how
            covered[hit["entity_id"]].append(f)
    return covered, unmatched, out_of_universe


def annotate(rows: list[dict]) -> dict[str, dict]:
    """
    filing_id -> {entity_id, filing_unit, match_how} for a list of filings.

    The same matcher `main` uses, exposed so the SQL export attributes filings
    exactly the way the coverage ledger does. Two implementations of "which
    entity is this?" would drift, and the first sign of the drift would be a
    site page disagreeing with its own database.
    """
    entities = load_entities()
    by_id = {e["entity_id"]: e for e in entities}
    covered, _unmatched, _out = match(list(rows), entities)
    out: dict[str, dict] = {}
    for entity_id, filings in covered.items():
        unit = by_id[entity_id]["filing_unit"]
        for f in filings:
            fid = f.get("filing_id")
            if fid:
                out[fid] = {"entity_id": entity_id, "filing_unit": unit,
                            "match_how": f.get("_how")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", type=int, default=300)
    args = ap.parse_args()

    entities = load_entities()
    filings = load_filings()
    covered, unmatched, out_of_universe = match(filings, entities)

    OUT.mkdir(parents=True, exist_ok=True)
    ledger = []
    for e in entities:
        got = covered.get(e["entity_id"], [])
        ledger.append({
            "entity_id": e["entity_id"], "segment": e["segment"], "state": e["state"],
            "entity_name": e["entity_name"], "parent_org": e["parent_org"],
            "enrollment": e["enrollment"], "tier": e["tier"],
            "in_scope_cy2025": e["in_scope_cy2025"],
            "filings_held": len(got),
            "status": "covered" if got else "uncovered",
            "match_how": got[0].get("_how", "") if got else "",
            "source_url": got[0].get("source_url", "") if got else "",
        })
    with open(OUT / "coverage_ledger.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ledger[0]))
        w.writeheader()
        w.writerows(ledger)

    with open(OUT / "unmatched_filings.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["parent_org", "plan_name", "coverage_type",
                                           "state", "contract_id", "source_url", "_segment_file"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(unmatched)

    with open(OUT / "out_of_universe_filings.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["parent_org", "plan_name", "coverage_type", "state",
                                           "source_url", "_why_out"], extrasaction="ignore")
        w.writeheader()
        w.writerows(out_of_universe)

    # The work queue: uncovered, in scope, primary tier, biggest first. Enrollment
    # is the ordering because a contract with 300,000 members carries more of the
    # national picture than twenty with 800.
    todo = [r for r in ledger
            if r["status"] == "uncovered" and r["in_scope_cy2025"] == "yes" and r["tier"] == "primary"]
    todo.sort(key=lambda r: -(int(r["enrollment"]) if str(r["enrollment"]).isdigit() else 0))
    with open(OUT / "targets.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(todo[0]))
        w.writeheader()
        w.writerows(todo[:args.targets])

    scope = [r for r in ledger if r["in_scope_cy2025"] == "yes" and r["tier"] == "primary"]
    cov = sum(1 for r in scope if r["status"] == "covered")
    print(f"filings loaded      {len(filings)}")
    print(f"matched to entity   {len(filings) - len(unmatched) - len(out_of_universe)}")
    print(f"unmatched filings   {len(unmatched)}  -> out/preauth/unmatched_filings.csv")
    print(f"outside universe    {len(out_of_universe)}  -> out/preauth/out_of_universe_filings.csv")
    print(f"\nuniverse (in scope, primary) {len(scope)}")
    print(f"covered                      {cov}  ({cov / len(scope):.1%})")
    print(f"uncovered                    {len(scope) - cov}\n")
    per = defaultdict(lambda: [0, 0])
    for r in scope:
        per[r["segment"]][1] += 1
        if r["status"] == "covered":
            per[r["segment"]][0] += 1
    for k in sorted(per, key=lambda k: -per[k][1]):
        c, n = per[k]
        print(f"  {c:5d} / {n:<5d} {c / n:6.1%}  {k}")
    print(f"\ntop uncovered by enrollment -> out/preauth/targets.csv")
    for r in todo[:15]:
        print(f"  {str(r['enrollment']):>9}  {r['entity_id']:<18} {r['entity_name'][:44]:<44} {r['parent_org'][:26]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
