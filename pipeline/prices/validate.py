"""
Consistency rules for published prices.

Same idea as the prior authorization rules: a publisher is required to publish,
nobody checks whether what they published agrees with itself, so we do. Every
rule compares numbers from the same document, or two numbers the same hospital
published about the same service in two places.

A finding is:
    {scope, ref, code, rule, severity, detail}
scope   'mrf' | 'inpatient' | 'outpatient' | 'physician'
ref     the hospital seed id, the CCN, or the geography the rule fired on
severity 'error'  the numbers contradict each other
         'warn'   implausible, or a requirement of the template not met
         'info'   worth knowing, not wrong
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

STALE_DAYS = 366         # the rule requires an update at least annually
CHARGE_RATIO_HIGH = 3.0  # MRF gross vs Medicare average covered charge, same hospital, same DRG
TOL = 0.01               # 1% slack on min/max comparisons for rounding
ABS_TOL = 1.00           # and at least a dollar: files round drug prices to the cent, so 0.095 vs 0.10 is not a finding


def _f(scope, ref, code, rule, severity, detail):
    return {"scope": scope, "ref": ref, "code": code, "rule": rule, "severity": severity, "detail": detail}


def _money(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:,.0f}"


# ---------------------------------------------------------------------------
# hospital MRFs

def mrf_charge_rules(c: dict) -> list[dict]:
    out = []
    # the setting initial (/i /o /b /u) keeps the (scope, ref, code, rule)
    # identity unique when one code is listed under two settings
    sid, code = c["seed_id"], f"{c['code_type']} {c['code']}/{(c.get('setting') or 'u')[:1]}"
    gross, cash, mn, mx = c.get("gross"), c.get("cash"), c.get("min"), c.get("max")
    nmin, nmax, n = c.get("negotiated_min"), c.get("negotiated_max"), c.get("negotiated_n") or 0

    # One code + setting can cover several chargemaster lines — a modifier, a
    # billing class (professional vs facility), a drug unit — each with its own
    # gross, cash, minimum and maximum. The crawl aggregates them into one item
    # and keeps the first value seen for each item-level field, counting the
    # distinct values in *_variants. Comparing one line's negotiated rate with
    # another line's maximum is not an arithmetic check; it is a category
    # error, and an early build reported 130,000 of them. So every cross-field
    # rule below runs only when the fields it compares are unambiguous (one
    # distinct value); a mixed item gets the single 'vary' note instead.
    one = lambda k: (c.get(f"{k}_variants") or 0) <= 1

    if gross is not None and cash is not None and one("gross") and one("cash") and cash > gross * (1 + TOL) and cash - gross >= ABS_TOL:
        out.append(_f("mrf", sid, code, "mrf_cash_above_gross", "error",
                      f"discounted cash price {_money(cash)} exceeds gross charge {_money(gross)}"))
    if mn is not None and mx is not None and one("min") and one("max") and mn > mx * (1 + TOL) and mn - mx >= ABS_TOL:
        out.append(_f("mrf", sid, code, "mrf_min_above_max", "error",
                      f"de-identified minimum {_money(mn)} exceeds maximum {_money(mx)}"))
    if n and mn is not None and nmin is not None and one("min") and nmin < mn * (1 - TOL) and mn - nmin >= ABS_TOL:
        out.append(_f("mrf", sid, code, "mrf_negotiated_below_min", "error",
                      f"a negotiated rate of {_money(nmin)} is below the file's own minimum {_money(mn)}"))
    if n and mx is not None and nmax is not None and one("max") and nmax > mx * (1 + TOL) and nmax - mx >= ABS_TOL:
        out.append(_f("mrf", sid, code, "mrf_negotiated_above_max", "error",
                      f"a negotiated rate of {_money(nmax)} is above the file's own maximum {_money(mx)}"))
    if gross is not None and nmax is not None and one("gross") and nmax > gross * (1 + TOL) and nmax - gross >= ABS_TOL:
        out.append(_f("mrf", sid, code, "mrf_negotiated_above_gross", "warn",
                      f"highest negotiated rate {_money(nmax)} exceeds the gross charge {_money(gross)}"))
    if gross is not None and cash is not None and one("gross") and one("cash") and gross > 0 and cash / gross < 0.10:
        out.append(_f("mrf", sid, code, "mrf_cash_under_tenth_of_gross", "info",
                      f"cash price {_money(cash)} is {cash / gross:.0%} of the gross charge {_money(gross)}"))
    varying = [k for k in ("gross", "cash", "min", "max") if (c.get(f"{k}_variants") or 0) > 1]
    if varying:
        lines = max(c.get(f"{k}_variants") or 0 for k in varying)
        out.append(_f("mrf", sid, code, "mrf_item_fields_vary", "info",
                      f"this code and setting cover at least {lines} distinct chargemaster lines (modifier, billing class or unit); "
                      f"their {', '.join(varying)} differ, so no cross-line comparison was made for this item"))
    pa, est = c.get("pct_or_algo_n") or 0, c.get("estimated_n") or 0
    if pa and est < pa:
        out.append(_f("mrf", sid, code, "mrf_rate_without_estimate", "warn",
                      f"{pa - est} of {pa} percentage/algorithm rates carry no estimated dollar amount, "
                      f"which the template has required since 1 January 2025"))
    if not n and not pa and (c.get("payer_rows") or 0) == 0 and gross is not None:
        out.append(_f("mrf", sid, code, "mrf_no_payer_rates", "info",
                      f"item is listed with a gross charge and no payer-specific rate at all"))
    return out


# The item-level rules whose failures can be a file-wide pattern rather than an
# arithmetic slip: when most of a file fails the same comparison the same way,
# the file's column means something other than the template says (a visit-level
# cash package copied onto every line, a per-day figure, a shifted column —
# theirs or ours), and reporting it as hundreds of errors misstates it.
SYSTEMATIC_RULES = ("mrf_cash_above_gross", "mrf_negotiated_below_min", "mrf_negotiated_above_max", "mrf_min_above_max",
                    "mrf_negotiated_above_gross")
# "negotiated above gross" is a warning, not an error: a case rate or per-diem
# legitimately exceeds a per-line list price. File-wide it is a note about how
# the file is built; item by item it is noise.
SYSTEMATIC_SEVERITY = {"mrf_negotiated_above_gross": "warn"}
SYSTEMATIC_SHARE = 0.10   # of the items where the comparison was possible
SYSTEMATIC_MIN = 20       # and at least this many


def comparable(c: dict) -> set:
    """Which SYSTEMATIC_RULES could have fired on this item (the fields exist and are unambiguous)."""
    one = lambda k: (c.get(f"{k}_variants") or 0) <= 1
    n = c.get("negotiated_n") or 0
    out = set()
    if c.get("gross") is not None and c.get("cash") is not None and one("gross") and one("cash"):
        out.add("mrf_cash_above_gross")
    if c.get("gross") is not None and c.get("negotiated_max") is not None and one("gross"):
        out.add("mrf_negotiated_above_gross")
    if c.get("min") is not None and c.get("max") is not None and one("min") and one("max"):
        out.add("mrf_min_above_max")
    if n and c.get("min") is not None and c.get("negotiated_min") is not None and one("min"):
        out.add("mrf_negotiated_below_min")
    if n and c.get("max") is not None and c.get("negotiated_max") is not None and one("max"):
        out.add("mrf_negotiated_above_max")
    return out


def demote_systematic(findings: list[dict], charges: list[dict]) -> list[dict]:
    """
    Per file and rule: if the rule fired on SYSTEMATIC_SHARE or more of the
    items it could have fired on (and at least SYSTEMATIC_MIN), replace the
    item errors with info notes and add one file-level error describing the
    pattern. The item rows stay — the site shows them — but they count once.
    """
    possible: dict = {}
    for c in charges:
        for r in comparable(c):
            possible[(c["seed_id"], r)] = possible.get((c["seed_id"], r), 0) + 1
    fired: dict = {}
    for f in findings:
        if f["scope"] == "mrf" and f["rule"] in SYSTEMATIC_RULES:
            fired[(f["ref"], f["rule"])] = fired.get((f["ref"], f["rule"]), 0) + 1
    systematic = {k: n for k, n in fired.items() if n >= SYSTEMATIC_MIN and n / max(1, possible.get(k, n)) >= SYSTEMATIC_SHARE}
    if not systematic:
        return findings
    out = []
    example: dict = {}
    for f in findings:
        k = (f["ref"], f["rule"])
        if k in systematic:
            example.setdefault(k, f["detail"])
            f = dict(f, severity="info", detail=f["detail"] + " — part of a file-wide pattern, counted once at the file level")
        out.append(f)
    for (sid, rule), n in sorted(systematic.items()):
        out.append(_f("mrf", sid, "", rule + "_systematic", SYSTEMATIC_SEVERITY.get(rule, "error"),
                      f"{n:,} of {possible.get((sid, rule), n):,} comparable items ({n / max(1, possible.get((sid, rule), n)):.0%}) fail this check the same way — "
                      f"the column does not mean what the template says it means, for this file or for this reader; e.g. {example[(sid, rule)]}"))
    return out


def collapse_estimates(findings: list[dict], charges: list[dict]) -> list[dict]:
    """
    'Percentage rate with no dollar estimate' is a property of how a file was
    built, not of an item: in the first crawl every file that had percentage
    or algorithm rates had no estimated amounts on any of them. One finding per
    file, with the counts, says that; 147,000 item rows saying it again do not.
    """
    per_file: dict = {}
    for c in charges:
        pa, est = c.get("pct_or_algo_n") or 0, c.get("estimated_n") or 0
        if pa:
            t = per_file.setdefault(c["seed_id"], [0, 0, 0])
            t[0] += pa; t[1] += est; t[2] += 1
    out = [f for f in findings if f["rule"] != "mrf_rate_without_estimate"]
    for sid, (pa, est, items) in sorted(per_file.items()):
        if est < pa:
            out.append(_f("mrf", sid, "", "mrf_rate_without_estimate", "warn",
                          f"{pa - est:,} of {pa:,} percentage/algorithm rates across {items:,} items carry no estimated dollar amount, "
                          f"which the template has required since 1 January 2025"
                          + (" — none of them do" if est == 0 else "")))
    return out


def _parse_date(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s[:len(fmt) + 4] if "T" in fmt else s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def mrf_file_rules(f: dict, today: Optional[dt.date] = None) -> list[dict]:
    out = []
    sid = f["seed_id"]
    if f.get("status") not in ("ok", "parsed_empty"):
        return out
    today = today or dt.date.today()
    d = _parse_date(f.get("last_updated_on"))
    if f.get("last_updated_on") and d is None:
        out.append(_f("mrf", sid, None, "mrf_unparseable_date", "info",
                      f"last_updated_on is '{f['last_updated_on']}', not a recognisable date"))
    elif d is None:
        out.append(_f("mrf", sid, None, "mrf_no_update_date", "warn",
                      "the file carries no last_updated_on; the template requires one"))
    elif (today - d).days > STALE_DAYS:
        out.append(_f("mrf", sid, None, "mrf_stale", "warn",
                      f"last updated {d.isoformat()}, {(today - d).days} days ago; "
                      f"the rule requires an update at least once a year"))
    elif d > today:
        out.append(_f("mrf", sid, None, "mrf_future_date", "warn",
                      f"last_updated_on {d.isoformat()} is in the future"))
    v = (f.get("version") or "").strip()
    if not v:
        out.append(_f("mrf", sid, None, "mrf_no_version", "info", "the file states no template version"))
    elif not re.match(r"^v?[23]\.\d", v):
        # v2.x (2024) and v3.0 (2025) are current; v1 is the pre-2024 template
        out.append(_f("mrf", sid, None, "mrf_old_template", "info",
                      f"file declares template version '{v}'; the current templates are 2.x and 3.x"))
    aff = (f.get("affirmation") or "").strip().lower()
    # The crawler does not yet read the affirmation cell from every layout, so
    # an absent value says nothing about the file; only an explicit non-true
    # value is a finding.
    if aff and aff not in ("true", "yes", "1") and "affirm" not in aff:
        out.append(_f("mrf", sid, None, "mrf_no_affirmation", "info",
                      "the file does not carry the affirmation statement the template requires"))
    return out


def mrf_vs_medicare(charges: list[dict], files: list[dict], inpatient: list[dict]) -> list[dict]:
    """
    Two numbers the same hospital published for the same DRG: the gross charge in
    its price file, and the average charge it submitted to Medicare. They are
    not the same quantity (one is a list price, the other an average of bills),
    but when one is three times the other, something in one of them is off.
    """
    out = []
    ccn_by_seed = {f["seed_id"]: f.get("ccn") for f in files if f.get("ccn")}
    med = {(r["ccn"], r["drg"]): r for r in inpatient}
    for c in charges:
        if c["code_type"] != "MS-DRG" or c.get("gross") is None:
            continue
        ccn = ccn_by_seed.get(c["seed_id"])
        if not ccn:
            continue
        m = med.get((ccn, c["code"]))
        if not m or not m.get("avg_covered_charge"):
            continue
        ratio = c["gross"] / m["avg_covered_charge"]
        if ratio > CHARGE_RATIO_HIGH or ratio < 1 / CHARGE_RATIO_HIGH:
            out.append(_f("mrf", c["seed_id"], f"MS-DRG {c['code']}/{(c.get('setting') or 'u')[:1]}",
                          "mrf_vs_medicare_charge", "warn",
                          f"price file lists a gross charge of {_money(c['gross'])}; the same hospital's "
                          f"average submitted charge to Medicare for this DRG is "
                          f"{_money(m['avg_covered_charge'])} ({ratio:.1f}×)"))
    return out


# ---------------------------------------------------------------------------
# Medicare files — the arithmetic inside one row

def inpatient_rules(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        tot, med, chg = r.get("avg_total_payment"), r.get("avg_medicare_payment"), r.get("avg_covered_charge")
        if tot is not None and med is not None and med > tot * (1 + TOL):
            out.append(_f("inpatient", r["ccn"], f"MS-DRG {r['drg']}", "inp_medicare_exceeds_total", "error",
                          f"Medicare's share {_money(med)} exceeds the total payment {_money(tot)}"))
        if tot is not None and chg is not None and tot > chg * (1 + TOL):
            out.append(_f("inpatient", r["ccn"], f"MS-DRG {r['drg']}", "inp_payment_exceeds_charge", "info",
                          f"total payment {_money(tot)} exceeds the average charge {_money(chg)}"))
        if r.get("charge_to_payment") is not None and r["charge_to_payment"] >= 10:
            out.append(_f("inpatient", r["ccn"], f"MS-DRG {r['drg']}", "inp_charge_10x_payment", "info",
                          f"average charge {_money(chg)} is {r['charge_to_payment']:.1f}× the total payment"))
    return out


def outpatient_rules(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        alw, pay, chg = r.get("avg_allowed"), r.get("avg_medicare_payment"), r.get("avg_submitted_charge")
        if alw is not None and pay is not None and pay > alw * (1 + TOL):
            out.append(_f("outpatient", r["ccn"], f"APC {r['apc']}", "out_payment_exceeds_allowed", "error",
                          f"Medicare payment {_money(pay)} exceeds the allowed amount {_money(alw)}"))
        if alw is not None and chg is not None and alw > chg * (1 + TOL):
            out.append(_f("outpatient", r["ccn"], f"APC {r['apc']}", "out_allowed_exceeds_charge", "info",
                          f"allowed amount {_money(alw)} exceeds the submitted charge {_money(chg)}"))
    return out


def physician_rules(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        alw, pay, chg = r.get("avg_allowed"), r.get("avg_medicare_payment"), r.get("avg_submitted_charge")
        ref = f"{r['geo']}/{r['place_of_service']}"
        if alw is not None and pay is not None and pay > alw * (1 + TOL):
            out.append(_f("physician", ref, r["hcpcs"], "phy_payment_exceeds_allowed", "error",
                          f"Medicare payment {_money(pay)} exceeds the allowed amount {_money(alw)}"))
        if alw is not None and chg is not None and alw > chg * (1 + TOL):
            out.append(_f("physician", ref, r["hcpcs"], "phy_allowed_exceeds_charge", "info",
                          f"allowed amount {_money(alw)} exceeds the submitted charge {_money(chg)}"))
    return out


def run(charges: list[dict], files: list[dict], inpatient: list[dict],
        outpatient: list[dict], physician: list[dict]) -> list[dict]:
    findings: list[dict] = []
    for c in charges:
        findings.extend(mrf_charge_rules(c))
    findings = demote_systematic(findings, charges)
    findings = collapse_estimates(findings, charges)
    for f in files:
        findings.extend(mrf_file_rules(f))
    findings.extend(mrf_vs_medicare(charges, files, inpatient))
    findings.extend(inpatient_rules(inpatient))
    findings.extend(outpatient_rules(outpatient))
    findings.extend(physician_rules(physician))
    return findings
