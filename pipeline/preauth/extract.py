"""
Read stored documents into filing records, one template at a time.

The project's standing rule is that a model reads and code does the arithmetic.
This module is the second half of that rule made cheap: where a payer publishes
the same layout dozens of times -- and the large ones all do, because they built
one template and stamped it per contract -- a parser written once against that
layout turns forty documents into forty filings with no transcription at all,
and no chance of a transposed digit.

A template is only added after its layout has been read by eye and confirmed
regular. Anything the parser cannot find comes back as None, never as zero: a
payer that published no appeal counts and a payer that received no appeals are
different claims and only one of them is interesting.

    python3 -m pipeline.preauth.extract humana        # writes data/seg_w5_humana.json
    python3 -m pipeline.preauth.extract --list        # templates and how many docs match

Every record carries source_url and the SHA-256 of the bytes it was read from,
so any figure can be traced back to the document as served.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "data" / "raw" / "preauth" / "docs"
DATA = ROOT / "data"

STATE_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Puerto Rico": "PR", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def num(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.replace(",", "").strip()
    return int(s) if s.isdigit() else None


def dec(s: str | None) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def stored() -> list[tuple[dict, str]]:
    """
    Every document in the store that rendered to text, with that text.

    Deduplicated on SHA-256: Wellcare serves the same PDF from www, chk and ilc
    hosts, and three URLs to one document must not become three filings. The
    first URL alphabetically keeps the document; the merge layer never sees the
    mirrors.
    """
    out, seen = [], set()
    for meta_path in sorted(DOCS.glob("*/SOURCE.json")):
        meta = json.loads(meta_path.read_text())
        tpath = meta_path.parent / "text.txt"
        if meta.get("error") or not tpath.exists():
            continue
        sha = meta.get("sha256")
        if sha and sha in seen:
            continue
        seen.add(sha)
        out.append((meta, tpath.read_text()))
    return out


# --------------------------------------------------------------------------
# Humana.
#
# One PDF per MA contract and one per Medicaid state, 42 of them, every one the
# same five-page template: two count tables per request class, an appeals table
# on the standard side only, and a turnaround table. The header carries either
# "Contract Number:" (Medicare Advantage) or "State:" (Medicaid), which is also
# what decides the market. The service list is a URL printed under the heading,
# not a table -- so it is captured as a link, which is what the rule requires the
# payer to publish and what we did not previously collect at all.
# --------------------------------------------------------------------------
def humana_match(meta: dict, text: str) -> bool:
    return ("Contract Number:" in text or re.search(r"^State:", text, re.M)) \
        and "Prior Authorization Metrics" in text \
        and "humana" in meta["url"].lower()


def humana_parse(meta: dict, text: str) -> list[dict]:
    pairs = re.findall(r"Request (approved|denied)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%", text)
    ext = re.findall(r"Request (approved|denied)(?: only)? after time for\s*\n\s*"
                     r"([\d,]+)\s+([\d,]+)\s+([\d.]+)%", text)
    app = re.findall(r"Request (approved only|denied) after appeal\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%", text)

    # pairs comes out in document order: standard approved, standard denied,
    # expedited approved, expedited denied. Fewer than four means the layout is
    # not what we think it is, and guessing which two we got would be worse than
    # refusing.
    if len(pairs) < 4:
        return []
    std_app, std_den, exp_app, exp_den = pairs[0], pairs[1], pairs[2], pairs[3]

    # The turnaround table's row label wraps around its own numbers -- the layout
    # is "Standard (non-urgent) prior / <mean> <median> / authorization requests"
    # -- so the numbers are anchored on the first half of the label, not the last.
    tat_d = re.search(r"non-urgent\) prior\s*\n\s*([\d.]+) day\(s\)\s+([\d.]+) day\(s\)", text)
    tat_h = re.search(r"urgent\) prior\s*\n\s*([\d.]+) hour\(s\)\s+([\d.]+) hour\(s\)", text)

    cid = re.search(r"Contract Number:\s*([A-Z0-9]+)", text)
    st = re.search(r"^State:\s*(.+)$", text, re.M)
    period = re.search(r"Reporting Period:\s*(.+)", text)
    svc = re.search(r"prior authorization \(excluding drugs\)\s*\n+\s*(\S+)", text)

    state = STATE_CODE.get(st.group(1).strip()) if st else None
    if cid:
        coverage, name = "Medicare Advantage", f"Humana {cid.group(1)}"
    else:
        coverage = "Medicaid Managed Care"
        name = f"Humana Healthy Horizons in {st.group(1).strip()}" if st else "Humana"

    return [{
        "parent_org": "Humana",
        "plan_name": name,
        "contract_id": cid.group(1) if cid else None,
        "coverage_type": coverage,
        "state": state,
        "reporting_period": period.group(1).strip() if period else None,
        "std_total": num(std_app[2]),
        "std_approved": num(std_app[1]),
        "std_denied": num(std_den[1]),
        "std_denied_pct": dec(std_den[3]),
        "std_appeals_total": num(app[0][2]) if app else None,
        "std_appeals_overturned": num(app[0][1]) if app else None,
        "ext_review_approved": num(ext[0][1]) if ext else None,
        "exp_total": num(exp_app[2]),
        "exp_approved": num(exp_app[1]),
        "exp_denied": num(exp_den[1]),
        "exp_denied_pct": dec(exp_den[3]),
        "std_tat_mean_days": dec(tat_d.group(1)) if tat_d else None,
        "std_tat_median_days": dec(tat_d.group(2)) if tat_d else None,
        "exp_tat_mean_hours": dec(tat_h.group(1)) if tat_h else None,
        "exp_tat_median_hours": dec(tat_h.group(2)) if tat_h else None,
        "reports_counts": bool(std_app is not None or exp_app is not None),
        "service_list_url": svc.group(1) if svc else None,
        "source_url": meta["url"],
        "source_sha256": meta["sha256"],
        "extraction_note": "parsed from the published template by "
                           "pipeline.preauth.extract (humana)",
    }]



# --------------------------------------------------------------------------
# The CMS template.
#
# CMS published a reporting template with the rule, and a large share of payers
# filled it in rather than designing their own. That is the single most useful
# fact about this corpus: the same six row labels appear in document after
# document, across payers with nothing else in common --
#
#     Request approved
#     Request approved only after time for review was extended
#     Request approved only after appeal
#     Request denied
#     Request denied after time for review was extended
#     Request denied after appeal
#
# -- once for non-urgent requests and once for urgent, followed by a turnaround
# table. Payers rearrange the layout freely (Humana splits it across three
# tables per class; Blue Cross of Kansas keeps one flat table) and pdftotext
# wraps labels around their own numbers, so matching on layout does not travel.
# Matching on the labels does.
#
# The parser therefore works backwards from the numbers: find every
# count/total/percentage triple, look at the text around it, and let the nearest
# label phrase say which row it is. A triple whose nearest label is ambiguous is
# dropped rather than assigned. Every section is then checked -- approved plus
# denied must equal the total the payer itself printed -- and a section that
# fails is returned with a note instead of silently standing.
# --------------------------------------------------------------------------
CMS_LABELS = [
    # Timeliness rows first. Several payers add "Requests approved within 14
    # days" beside the real approval row; the words overlap, and a triple that
    # lands on one of these must be recognised and then ignored rather than
    # mistaken for the approval count.
    # "within" alone is the tell -- the unit is often wrapped onto the next line
    # and never appears beside the numbers, so it cannot be required here.
    ("timely_approved", r"approved within \d"),
    ("timely_denied", r"denied within \d"),

    ("appeal_approved", r"approved[^\n]{0,25}only after appeal|approved[^\n]{0,25}after appeal"),
    ("appeal_denied", r"denied[^\n]{0,25}after appeal"),
    # Two wordings for the same row: the CMS template's "after time for review
    # was extended", and the shorter "after extension" several states prefer.
    ("ext_approved", r"approved only after time for\s+review was extended|"
                     r"approved only after time for review was extended|"
                     r"approved[^\n]{0,15}after time for\s*\n?\s*review was extended|"
                     r"approved[^\n]{0,10}after\s*\n?\s*extension"),
    ("ext_denied", r"denied[^\n]{0,15}after time for\s*\n?\s*review was extended|"
                   r"denied[^\n]{0,10}after\s*\n?\s*extension"),
    ("approved", r"requests? approved"),
    ("denied", r"requests? denied"),
]

TRIPLE = re.compile(r"([\d,]+)\s+([\d,]+)\s+([\d.]+)\s*%")

# A percentage standing on its own, for the payers who published rates and no
# counts at all. The rule lets them: it asks for "percentage and number", and a
# payer that prints only the percentage is under-reporting rather than silent.
# Those filings are worth keeping -- 119 rows in the existing dataset are
# exactly this -- as long as the counts stay NULL and reports_counts says so.
LONE_PCT = re.compile(r"(?<![\d,])([\d]{1,3}(?:\.\d+)?)\s*%")

# Headings that switch which request class the following numbers belong to.
# "Non-Urgent" contains "Urgent", and a hyphen is a word boundary, so the urgent
# pattern has to refuse the negated form explicitly or every standard heading
# reads as an expedited one.
URGENT_HEAD = re.compile(r"(?im)^[^\n]*\b(?<!non-)(?<!non )(expedited|urgent)\b[^\n]*"
                         r"prior auth[^\n]*$")
STD_HEAD = re.compile(r"(?im)^[^\n]*\b(standard|non-?urgent)\b[^\n]*prior auth[^\n]*$")


def _class_at(text: str, pos: int) -> str:
    """Which request class the numbers at `pos` belong to: the nearest heading above."""
    last, kind = -1, "std"
    for m in STD_HEAD.finditer(text):
        if m.start() < pos and m.start() > last:
            last, kind = m.start(), "std"
    for m in URGENT_HEAD.finditer(text):
        if m.start() < pos and m.start() > last:
            last, kind = m.start(), "exp"
    return kind


def _label_at(text: str, start: int, end: int) -> str | None:
    """
    Which row these numbers belong to.

    A table row puts its label on the same line as its numbers, so that line is
    checked first and wins outright. When the label is too long for the column,
    pdftotext wraps it around the numbers -- the label's first half on the line
    above, its second half on the line below -- so the neighbouring lines are
    checked next, nearest first. Anything further away than that is not this
    row's label and the numbers are dropped rather than attributed.
    """
    lines = text.splitlines(keepends=True)
    offs, acc = [], 0
    for ln in lines:
        offs.append(acc)
        acc += len(ln)
    idx = max(i for i, o in enumerate(offs) if o <= start)

    # Whitespace is collapsed before matching. pdftotext pads table cells out
    # with runs of spaces, and a label that reads "review was extended" on the
    # page arrives as "review    was          extended"; the patterns are
    # written the way a person would write them, so the text has to be made to
    # match that rather than the other way round.
    def flat(x: str) -> str:
        return " ".join(x.split())

    same = flat(TRIPLE.sub(" ", lines[idx]))
    above = flat(lines[idx - 1]) if idx > 0 else ""
    below = flat(lines[idx + 1]) if idx + 1 < len(lines) else ""
    above2 = flat(lines[idx - 2]) if idx > 1 else ""

    specific = [(n, p) for n, p in CMS_LABELS if n not in ("approved", "denied")]
    bare = [(n, p) for n, p in CMS_LABELS if n in ("approved", "denied")]

    def find(pool, hay: str, must_start_before: int | None = None) -> str | None:
        """
        First pattern in `pool` that matches `hay`.

        `must_start_before` is what keeps a neighbouring row from stealing this
        one's label. When two lines are joined to catch a label that wrapped,
        the match has to *begin* in this row's own line -- a qualified label
        that starts on the next line down belongs to the next row, not this one.
        """
        low = hay.lower()
        for name, pat in pool:
            m = re.search(pat, low)
            if m and (must_start_before is None or m.start() < must_start_before):
                return name
        return None

    # Order matters, and the reason is a genuine conflict between two real
    # layouts. "Request approved after / extension" wraps its qualifier onto the
    # next line, so a bare match on the first line alone would misread it as the
    # plain approval row. But a flat table puts "Request denied" on one line and
    # "Request denied after time for review was extended" on the next, so a
    # widened qualified search would misread *that* as the extension row. The
    # start-position rule is what lets both be right.
    hit = find(specific, same)
    if hit:
        return hit
    hit = find(specific, same + " " + below, must_start_before=len(same))
    if hit:
        return hit
    hit = find(bare, same)
    if hit:
        return hit
    # The label wrapped around its own numbers: its head is on the line above.
    hit = find(specific, above + " " + below)
    if hit:
        return hit
    hit = find(specific, above2 + " " + above + " " + below)
    if hit:
        return hit
    for probe in (above + " " + below, above, below):
        hit = find(bare, probe)
        if hit:
            return hit
    return None


def cms_match(meta: dict, text: str) -> bool:
    low = text.lower()
    return (re.search(r"requests? approved", low) is not None
            and re.search(r"requests? denied", low) is not None
            and "prior authorization" in low
            and bool(TRIPLE.search(text)))


def _tat(text: str) -> dict:
    """
    Mean and median turnaround, per request class, in whatever unit was printed.

    The class comes from the row's own label rather than the section heading
    above it: the turnaround table sits at the end of the document, under
    whichever heading happened to come last, and both its rows belong to
    different classes anyway.
    """
    out = {"std_tat_mean_days": None, "std_tat_median_days": None,
           "exp_tat_mean_hours": None, "exp_tat_median_hours": None}
    lines = text.splitlines(keepends=True)
    offs, acc = [], 0
    for ln in lines:
        offs.append(acc)
        acc += len(ln)

    for m in re.finditer(r"([\d.]+)\s*(day|hour)s?\b[^\n\d]*?([\d.]+)\s*(day|hour)s?\b", text):
        mean, unit, med = m.group(1), m.group(2), m.group(3)
        i = max(j for j, o in enumerate(offs) if o <= m.start())
        near = " ".join(lines[max(0, i - 2): i + 3]).lower()
        if re.search(r"non-?urgent|standard", near):
            kind = "std"
        elif re.search(r"expedited|urgent", near):
            kind = "exp"
        else:
            kind = "std" if unit == "day" else "exp"
        if kind == "std" and out["std_tat_mean_days"] is None:
            # a standard turnaround printed in hours is still a standard
            # turnaround; convert rather than drop it, and say so nowhere else
            # because the column name already carries the unit.
            f = 1 / 24 if unit == "hour" else 1
            out["std_tat_mean_days"] = round(dec(mean) * f, 3) if dec(mean) is not None else None
            out["std_tat_median_days"] = round(dec(med) * f, 3) if dec(med) is not None else None
        elif kind == "exp" and out["exp_tat_mean_hours"] is None:
            f = 24 if unit == "day" else 1
            out["exp_tat_mean_hours"] = round(dec(mean) * f, 3) if dec(mean) is not None else None
            out["exp_tat_median_hours"] = round(dec(med) * f, 3) if dec(med) is not None else None
    return out


def _service_list(text: str) -> tuple[str | None, int | None]:
    """
    The rule's other mandated artifact: the list of items and services requiring
    prior authorization. Payers publish it either as a link or as an inline list,
    and both are worth keeping -- a link is a pointer to a document we can fetch
    later, an inline list is the thing itself.
    """
    m = re.search(r"require prior authorization \(excluding drugs\)(.{0,900})", text, re.S | re.I)
    if not m:
        return None, None
    block = m.group(1)
    url = re.search(r"(https?://\S+|www\.\S+)", block)
    if url:
        return url.group(1).rstrip(".,"), None
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    items = []
    for ln in lines:
        if re.search(r"prior to january|beginning january|final rule|timeframes|^\d+$", ln, re.I):
            break
        items.append(ln)
    return None, len(items) or None


# Publishers whose documents never name their own state or market. The domain
# is the identification; nothing here invents a number.
DOMAIN_HINTS = {
    "healthfirst.org": {"parent_org": "Healthfirst", "state": "NY"},
    "azblue.com": {"parent_org": "Blue Cross Blue Shield of Arizona", "state": "AZ"},
    "/anthem/medicaid/in/": {"parent_org": "Elevance Health", "state": "IN",
                             "coverage_type": "Medicaid Managed Care"},
    "lacare.org": {"parent_org": "L.A. Care Health Plan", "state": "CA"},
    "nhprae2.org": {"parent_org": "Northeast Health Partners", "state": "CO"},
    "dhhs.utah.gov": {"parent_org": "Utah Department of Health and Human Services",
                      "state": "UT", "coverage_type": "Medicaid FFS"},
    # AmeriHealth Caritas trades under a different name in several states, and
    # those pages carry the brand rather than the state or the parent.
    "keystonefirstchip.com": {"parent_org": "AmeriHealth Caritas", "state": "PA",
                              "coverage_type": "CHIP Managed Care"},
    "keystonefirstpa.com": {"parent_org": "AmeriHealth Caritas", "state": "PA"},
    "performcare.org": {"parent_org": "AmeriHealth Caritas", "state": "PA"},
    "selecthealthofsc.com": {"parent_org": "AmeriHealth Caritas", "state": "SC"},
    "firstchoicenext.com": {"parent_org": "AmeriHealth Caritas", "state": "SC"},
    "blueshieldca.com": {"parent_org": "Blue Shield of California",
                         "plan_name": "Blue Shield of California Promise Health Plan",
                         "state": "CA", "coverage_type": "Medicaid Managed Care"},
    "alliancehealthplan.org": {"parent_org": "Alliance Health", "plan_name": "Alliance Health",
                               "state": "NC", "coverage_type": "Medicaid Managed Care"},
    "medicaid.ms.gov": {"state": "MS", "coverage_type": "Medicaid Managed Care"},
    "chpw.org": {"parent_org": "Community Health Plan of Washington",
                 "plan_name": "Community Health Plan of Washington",
                 "state": "WA", "coverage_type": "Medicaid Managed Care"},
    "amerihealthcaritas": {"parent_org": "AmeriHealth Caritas"},
}


def cms_parse(meta: dict, text: str) -> list[dict]:
    # A consolidated document -- one PDF carrying dozens of contracts -- must
    # not be collapsed into a single filing by first-occurrence-wins. Those
    # documents get their own parsers (uhc, kaiser) or a collector pass; the
    # generic template refuses them rather than misreading them.
    if len(TRIPLE.findall(text)) > 24:
        return []

    got: dict[tuple, tuple] = {}
    for m in TRIPLE.finditer(text):
        label = _label_at(text, m.start(), m.end())
        if not label:
            continue
        key = (_class_at(text, m.start()), label)
        if key not in got:                       # first occurrence wins
            got[key] = (num(m.group(1)), num(m.group(2)), dec(m.group(3)))

    def cell(kind: str, label: str, idx: int = 0):
        v = got.get((kind, label))
        return v[idx] if v else None

    std_app, std_den = cell("std", "approved"), cell("std", "denied")
    exp_app, exp_den = cell("exp", "approved"), cell("exp", "denied")

    # Percentage-only fallback. Only consulted when no count table was found at
    # all, so a document that publishes both is never read twice.
    pct_only = {}
    if std_app is None and exp_app is None:
        spans = [(m.start(), m.end()) for m in TRIPLE.finditer(text)]
        for m in LONE_PCT.finditer(text):
            if any(a <= m.start() < b for a, b in spans):
                continue
            label = _label_at(text, m.start(), m.end())
            if label in (None, "timely_approved", "timely_denied"):
                continue
            key = (_class_at(text, m.start()), label)
            pct_only.setdefault(key, dec(m.group(1)))
        if not any(k[1] in ("approved", "denied") for k in pct_only):
            return []

    # Two different failures wear the same face: a parse that picked the wrong
    # rows, and a payer whose own counts do not add up. They are told apart by
    # the percentage the payer printed beside each count -- if count/total
    # reproduces that percentage, the rows were read correctly and any shortfall
    # against the total is the payer's arithmetic, which is a finding worth
    # publishing. If the percentage does not reproduce, the parse is suspect and
    # the row is held back instead.
    notes, suspect = [], False
    std_total = cell("std", "approved", 1) or cell("std", "denied", 1)
    exp_total = cell("exp", "approved", 1) or cell("exp", "denied", 1)

    def prints_right(kind: str, label: str) -> bool | None:
        v = got.get((kind, label))
        if not v or not v[1] or v[2] is None:
            return None
        return abs(v[0] / v[1] * 100 - v[2]) <= 0.6

    for kind, a, d, t in (("standard", std_app, std_den, std_total),
                          ("expedited", exp_app, exp_den, exp_total)):
        if None in (a, d, t) or a + d == t:
            continue
        k = "std" if kind == "standard" else "exp"
        checks = [prints_right(k, "approved"), prints_right(k, "denied")]
        # Approved and denied are two rows of one table and must be out of the
        # same denominator. When they are not, the two triples came from
        # different tables however well each one's own percentage checks out.
        same_base = (got.get((k, "approved")) or (None, None))[1] == \
                    (got.get((k, "denied")) or (None, None))[1]
        if same_base and all(c is True for c in checks):
            notes.append(f"{kind}: the payer's own counts do not add up -- approved "
                         f"({a:,}) plus denied ({d:,}) is {a + d:,} against a printed "
                         f"total of {t:,}; both percentages match the counts as printed")
        else:
            suspect = True
            notes.append(f"{kind}: approved + denied ({a:,} + {d:,}) does not equal the "
                         f"printed total ({t:,}), and the printed percentages do not "
                         f"confirm the rows -- read this one by hand")

    # Almost every filled-in template carries the sentence "<payer> is required
    # to annually report ...". The payer's name is whatever sits between the end
    # of the rule's own name and that phrase -- across a line break, sometimes in
    # square brackets where a template placeholder was filled in but not tidied.
    payer = None
    m = re.search(r"(.{3,200}?)\s+is\s+required\s+to\s+annually\s+report", text, re.S)
    if m:
        name = re.split(r"(?i)final rule\s*(?:\([^)]*\))?[\s,]*", m.group(1))[-1]
        name = " ".join(name.strip(" []\n\t,.").split())
        payer = name if 3 <= len(name) <= 120 else None
    if not payer:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        payer = " ".join(first.split())[:120] or None

    # The document's own masthead is the most reliable place for the contract
    # ID ("2025 MEDICARE-H1994"); check it before the labelled forms.
    cid = (re.search(r"\b([HR]\d{4})\b", text[:200])
           or re.search(r"Contract Number:\s*([A-Z0-9]+)", text)
           or re.search(r"(?i)MA Contract\s*:\s*([HR]\d{4})", text)
           or re.search(r"(?i)\bcontract\s*(?:id|number|#)?\s*:?\s*([HR]\d{4})\b", text)
           or re.search(r"(?m)^\s*([HR]\d{4})_[A-Za-z0-9]+", text)
           or re.search(r"(?m)^\s*([HR]\d{4})\s*$", text))
    st = re.search(r"(?m)^State:\s*(.+)$", text)
    period = re.search(r"Reporting Period:\s*(.+)", text)
    svc_url, svc_items = _service_list(text)

    # Market and state, where the document does not label them. A state agency
    # naming itself in the boilerplate is a fee-for-service programme -- that is
    # who those agencies are under this rule -- and the state name in its own
    # title is the state. This classifies; it never invents a number.
    state = STATE_CODE.get(st.group(1).strip()) if st else None
    coverage = ("Medicare Advantage" if cid else "Medicaid Managed Care" if st else None)
    if coverage is None:
        # The URL names the product line more reliably than the prose does --
        # AZ Blue's two reports differ only by "MA" vs "ACA" in the filename.
        u = meta["url"].lower()
        head = text[:400].lower()
        if re.search(r"[-_/]ma[-_]|medicare", u) or "medicare advantage" in head:
            coverage = "Medicare Advantage"
        elif re.search(r"aca|marketplace|exchange", u + " " + head):
            coverage = "Marketplace QHP"
    if not cid and payer:
        agency = re.search(r"\b(department of health|health care authority|division of "
                           r"medicaid|bureau of medicaid|medicaid services|human services)\b",
                           payer, re.I)
        if agency:
            coverage = "Medicaid FFS"
            if state is None:
                for full, code in STATE_CODE.items():
                    if re.search(rf"\b{re.escape(full)}\b", payer, re.I):
                        state = code
                        break

    row = [{
        "parent_org": payer,
        "plan_name": payer,
        "contract_id": cid.group(1) if cid else None,
        "coverage_type": coverage,
        "state": state,
        "reporting_period": period.group(1).strip() if period else None,
        "std_total": std_total, "std_approved": std_app, "std_denied": std_den,
        "std_denied_pct": (got.get(("std", "denied")) or (None, None, None))[2]
                          if got.get(("std", "denied")) else pct_only.get(("std", "denied")),
        "exp_denied_pct": (got.get(("exp", "denied")) or (None, None, None))[2]
                          if got.get(("exp", "denied")) else pct_only.get(("exp", "denied")),
        "std_appeals_total": cell("std", "appeal_approved", 1),
        "std_appeals_overturned": cell("std", "appeal_approved"),
        "ext_review_approved": cell("std", "ext_approved"),
        "exp_total": exp_total, "exp_approved": exp_app, "exp_denied": exp_den,
        **_tat(text),
        "reports_counts": bool(std_app is not None or exp_app is not None),
        "service_list_url": svc_url,
        "service_list_items": svc_items,
        "source_url": meta["url"], "source_sha256": meta["sha256"],
        "extraction_note": "parsed from the CMS reporting template by "
                           "pipeline.preauth.extract (cms)"
                           + ("; " + "; ".join(notes) if notes else ""),
        "needs_review": suspect or None,
    }]
    for dom, hints in DOMAIN_HINTS.items():
        if dom not in meta["url"]:
            continue
        r = row[0]
        # The document's own first line names the product, not the payer; keep
        # it as the plan name and let the domain say who published it.
        if hints.get("parent_org") and r["parent_org"] != hints["parent_org"]:
            r["plan_name"] = r["plan_name"] or r["parent_org"]
            r["parent_org"] = hints["parent_org"]
        if re.search(r"(?i)^(2025 )?prior authorization|^standard \(non-urgent\)",
                     r.get("plan_name") or ""):
            # the plan name captured the report heading; the real product name
            # is the next non-heading line of the document (L.A. Care prints
            # "L.A. Care Medi-Cal" there), and failing that the payer name.
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            # A product name is words, not numbers: reject any candidate line
            # carrying a figure, which is what table rows are made of.
            better = next((ln for ln in lines[1:6]
                           if not re.search(r"(?i)prior authorization|metrics|"
                                            r"interoperab|how many|out of total|"
                                            r"percentage|requests?$|\d", ln)), None)
            r["plan_name"] = (better or r["parent_org"])[:120]
        for k, v in hints.items():
            if not r.get(k):
                r[k] = v
        if not r.get("coverage_type"):
            low = (r.get("plan_name") or "").lower()
            r["coverage_type"] = ("CHIP Managed Care" if "child health" in low or "chip" in low
                                  else "Medicaid Managed Care")
    return row



# --------------------------------------------------------------------------
# Kaiser Permanente.
#
# Kaiser publishes one report per region and puts every contract in it, which is
# why the generic parser could not read a word of it: there are no decision
# labels beside the numbers. The label is a section heading five lines up, the
# row label is the contract ID, and each row carries the approved side and the
# denied side of one metric as two count/total/percentage triples on one line:
#
#     Request Approved                        Request Denied
#     CMS Contract ID   n     of      pct     n      of      pct
#     H1170          49,908  51,523  96.87%  1,615  51,523   3.13%
#
# Four sections repeat per request class -- the base counts, a timeliness split,
# the extended-review pair and the appeal pair -- so the parser walks the
# document keeping track of which class and which section it is inside, and
# files each contract's numbers accordingly. One report yields every contract in
# the region rather than one filing.
# --------------------------------------------------------------------------
# Kaiser names its own regions, and two of them span states. A region that
# covers more than one state gets no state rather than an arbitrary one -- the
# same rule the national MA contracts follow.
KP_REGION = {
    "Northern California": "CA", "Southern California": "CA", "California": "CA",
    "Colorado": "CO", "Georgia": "GA", "Hawaii": "HI", "Maryland": "MD",
    "Virginia": "VA", "Oregon": "OR", "Washington": "WA",
    "Mid-Atlantic States": None, "Northwest": None,
}

KP_ROW = re.compile(
    r"^\s*([A-Z]\d{4}|[A-Z]{2,}\d*)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)\s*%"
    r"\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)\s*%\s*$")


def kaiser_match(meta: dict, text: str) -> bool:
    return "kaiserpermanente" in meta["url"].lower() and "CMS Contract ID" in text


def _kp_section(window: str) -> str | None:
    """Which metric pair the following rows belong to."""
    w = " ".join(window.lower().split())
    if "only after appeal" in w:
        return "appeal"
    if "after time for review" in w or "was extended" in w:
        return "ext"
    if re.search(r"within \d+\s*(day|hour)", w):
        return "timely"
    if "request approved" in w:
        return "base"
    return None


def kaiser_parse(meta: dict, text: str) -> list[dict]:
    region = re.search(r"KP Region:\s*(.+?)\s{2,}", text)
    period = re.search(r"Reporting Period:\s*(\S+)", text)
    rname = region.group(1).strip() if region else ""
    state = KP_REGION.get(rname, STATE_CODE.get(rname))

    lines = text.splitlines()
    cls, section = "std", None
    # data[(contract, class)][section] = (approved triple, denied triple)
    data: dict[tuple, dict] = {}
    market: dict[str, str] = {}
    current_market = "Medicare Advantage"

    for i, ln in enumerate(lines):
        low = ln.lower()
        if "expedited" in low and "urgent" in low and "prior authorization" in low:
            cls = "exp"
        elif "standard" in low and "prior authorization" in low:
            cls = "std"
        if "contract id level" in low:
            current_market = "Medicare Advantage"
        elif "medicaid" in low and "level" in low:
            current_market = "Medicaid Managed Care"

        sec = _kp_section(" ".join(lines[max(0, i - 1): i + 2]))
        if sec:
            section = sec

        m = KP_ROW.match(ln)
        if not m or section is None:
            continue
        cid = m.group(1)
        entry = data.setdefault((cid, cls), {})
        entry[section] = ((num(m.group(2)), num(m.group(3)), dec(m.group(4))),
                          (num(m.group(5)), num(m.group(6)), dec(m.group(7))))
        market[cid] = current_market

    out = []
    for cid in sorted({c for c, _ in data}):
        std = data.get((cid, "std"), {})
        exp = data.get((cid, "exp"), {})
        if "base" not in std and "base" not in exp:
            continue

        def side(d, sec, which, idx=0):
            v = d.get(sec)
            return v[0 if which == "a" else 1][idx] if v else None

        out.append({
            "parent_org": "Kaiser Permanente",
            "plan_name": f"Kaiser Permanente {region.group(1).strip()} {cid}"
                         if region else f"Kaiser Permanente {cid}",
            "contract_id": cid if re.match(r"^[HR]\d{4}$", cid) else None,
            "coverage_type": market.get(cid, "Medicare Advantage"),
            "state": state,
            "reporting_period": period.group(1).strip() if period else None,
            "std_total": side(std, "base", "a", 1),
            "std_approved": side(std, "base", "a"),
            "std_denied": side(std, "base", "d"),
            "std_denied_pct": side(std, "base", "d", 2),
            # The appeal row's denominator is appeals filed, and both sides carry
            # it; the approved side is the overturn.
            "std_appeals_total": side(std, "appeal", "a", 1),
            "std_appeals_overturned": side(std, "appeal", "a"),
            "ext_review_approved": side(std, "ext", "a"),
            "exp_total": side(exp, "base", "a", 1),
            "exp_approved": side(exp, "base", "a"),
            "exp_denied": side(exp, "base", "d"),
            "exp_denied_pct": side(exp, "base", "d", 2),
            "std_tat_mean_days": None, "std_tat_median_days": None,
            "exp_tat_mean_hours": None, "exp_tat_median_hours": None,
            "reports_counts": True,
            "service_list_url": None, "service_list_items": None,
            "source_url": meta["url"], "source_sha256": meta["sha256"],
            "extraction_note": "parsed from Kaiser's regional contract-level report by "
                               "pipeline.preauth.extract (kaiser). Turnaround times are not "
                               "published in these reports.",
        })
    return out



# --------------------------------------------------------------------------
# Anthem / Elevance, HTML state pages.
#
# Anthem's robots.txt disallows /*.pdf$ across the whole host, which puts every
# one of its PDF disclosures out of reach of any crawler that honours it. The
# HTML pages at /<state>/medicaid/prior-authorization-report are not covered by
# that rule, and they carry the same numbers.
#
# The layout is vertical rather than tabular: a decision label on its own line,
# then the count, the denominator and the percentage on the three lines after
# it. Each page then repeats the whole table transposed, which is why a row is
# only accepted when the three lines following the label are all numeric -- in
# the transposed copy the next line is another label, so it is skipped and the
# first, canonical block wins.
# --------------------------------------------------------------------------
ANTHEM_LABELS = [
    ("appeal_approved", re.compile(r"^request approved after appeal", re.I)),
    ("ext_approved", re.compile(r"^request approved (following|after) extended", re.I)),
    ("approved", re.compile(r"^request approved\s*$", re.I)),
    ("denied", re.compile(r"^request denied\s*$", re.I)),
]
NUMERIC = re.compile(r"^[\d,]+$|^[\d.]+\s*%$")


def anthem_match(meta: dict, text: str) -> bool:
    return ("anthem.com" in meta["url"].lower()
            and "prior-authorization-report" in meta["url"].lower()
            and "Number of times this happened" in text)


def anthem_parse(meta: dict, text: str) -> list[dict]:
    lines = [ln.strip() for ln in text.splitlines()]
    body = [ln for ln in lines if ln]
    got: dict[tuple, tuple] = {}
    cls = "std"
    for i, ln in enumerate(body):
        low = ln.lower()
        if "expedited" in low and "request" in low:
            cls = "exp"
        elif "standard" in low and "request" in low:
            cls = "std"
        for name, pat in ANTHEM_LABELS:
            if not pat.match(ln):
                continue
            trio = body[i + 1: i + 4]
            if len(trio) < 3 or not all(NUMERIC.match(x) for x in trio):
                continue
            got.setdefault((cls, name),
                           (num(trio[0]), num(trio[1]), dec(trio[2].rstrip("% "))))
            break

    def cell(c, n, idx=0):
        v = got.get((c, n))
        return v[idx] if v else None

    if cell("std", "approved") is None and cell("exp", "approved") is None:
        return []

    st = re.search(r"anthem\.com/([a-z]{2})/", meta["url"], re.I)
    state = st.group(1).upper() if st else None
    return [{
        "parent_org": "Elevance Health (Anthem)",
        "plan_name": f"Anthem Blue Cross and Blue Shield Medicaid — {state}",
        "contract_id": None,
        "coverage_type": "Medicaid Managed Care",
        "state": state,
        "reporting_period": "CY2025",
        "std_total": cell("std", "approved", 1) or cell("std", "denied", 1),
        "std_approved": cell("std", "approved"),
        "std_denied": cell("std", "denied"),
        "std_denied_pct": cell("std", "denied", 2),
        "std_appeals_total": cell("std", "appeal_approved", 1),
        "std_appeals_overturned": cell("std", "appeal_approved"),
        "ext_review_approved": cell("std", "ext_approved"),
        "exp_total": cell("exp", "approved", 1) or cell("exp", "denied", 1),
        "exp_approved": cell("exp", "approved"),
        "exp_denied": cell("exp", "denied"),
        "exp_denied_pct": cell("exp", "denied", 2),
        "std_tat_mean_days": None, "std_tat_median_days": None,
        "exp_tat_mean_hours": None, "exp_tat_median_hours": None,
        "reports_counts": True,
        "service_list_url": None, "service_list_items": None,
        "source_url": meta["url"], "source_sha256": meta["sha256"],
        "extraction_note": "parsed from Anthem's HTML state page by "
                           "pipeline.preauth.extract (anthem). Anthem's robots.txt "
                           "disallows every PDF on the host; these HTML pages are not "
                           "covered by that rule and carry the same figures. Turnaround "
                           "times are not published on them.",
    }]



# --------------------------------------------------------------------------
# The CMS numbered-item template.
#
# CMS's guidance lists the required metrics as a numbered series, and a second
# group of payers filled that in literally -- one metric per numbered line,
# value at the end, no table at all:
#
#     2a. The number of standard prior authorization requests that were approved, 390,769
#     2b. The total number of standard prior authorization requests, ...      425,335
#
# The cms table parser cannot see these because there is no count/total/percent
# triple to anchor on. The numbering is the anchor instead, and it is stable
# because it comes from the regulation's own ordering: 2 approved, 3 denied,
# 4 approved after appeal, 5 extended, 6 expedited approved, 7 expedited denied,
# 8 standard turnaround, 9 expedited turnaround.
#
# Item 5b is the combined standard+expedited denominator, so it is deliberately
# NOT read as a standard total -- that would inflate every standard rate.
# --------------------------------------------------------------------------
ITEM = re.compile(r"(?m)^\s*(\d{1,2})\s*([ab]?)\.\s*(.{0,150}?)[\s,]+([\d,]+(?:\.\d+)?)\s*(%?)\s*$")


def numbered_match(meta: dict, text: str) -> bool:
    # Payers post the same filing in several languages. They are one disclosure,
    # not several, and the translations carry the same numbers -- so only the
    # English edition is read and the rest are left alone.
    if re.search(r"(?i)[_-](sp|es|vi|zh|ko|ru|ar|ht|tl)\.(pdf|html?)$|/(es|vi|zh|ko|ru|ar)/",
                 meta["url"]):
        return False
    if re.search(r"(?i)autorizaci|solicitudes|porcentaje", text[:3000]):
        return False
    # A document with the Elevance scorecard header goes to that template: the
    # numbered items are the same, but the header names the plan and the market
    # state, and this parser has nowhere to read those from.
    if SCORE_HEAD.search(text[:2000]):
        return False
    keys = {(m.group(1) + m.group(2)) for m in ITEM.finditer(text)}
    return len({"2a", "2b", "3a", "6a"} & keys) >= 3


def numbered_parse(meta: dict, text: str) -> list[dict]:
    got: dict[str, float] = {}
    for m in ITEM.finditer(text):
        key = m.group(1) + m.group(2)
        val = dec(m.group(4).replace(",", ""))
        if val is None or key in got:
            continue
        got[key] = val

    def i(k):
        v = got.get(k)
        return int(v) if v is not None else None

    std_total = i("2b") or i("3b")
    exp_total = i("6b") or i("7b")
    payer = re.search(r"final rule,?\s+(.{3,80}?)\s+is required to annually report", text, re.S)
    st = re.search(r"(?m)^State:\s*(.+)$", text)
    cid = re.search(r"Contract Number:\s*([A-Z0-9]+)", text)
    period = re.search(r"Reporting Period:\s*(.+)", text)
    svc_url, svc_items = _service_list(text)

    notes = []
    if None not in (i("2a"), i("3a"), std_total) and i("2a") + i("3a") != std_total:
        notes.append(f"standard: approved + denied ({i('2a'):,} + {i('3a'):,}) does not equal "
                     f"the printed total ({std_total:,})")

    row = [{
        "parent_org": " ".join(payer.group(1).split()) if payer else None,
        "plan_name": " ".join(payer.group(1).split()) if payer else None,
        "contract_id": cid.group(1) if cid else None,
        "coverage_type": ("Medicare Advantage" if cid else "Medicaid Managed Care" if st else None),
        "state": STATE_CODE.get(st.group(1).strip()) if st else None,
        "reporting_period": period.group(1).strip() if period else None,
        "std_total": std_total, "std_approved": i("2a"), "std_denied": i("3a"),
        "std_appeals_total": i("4b"), "std_appeals_overturned": i("4a"),
        "ext_review_approved": i("5a"),
        "exp_total": exp_total, "exp_approved": i("6a"), "exp_denied": i("7a"),
        "std_tat_mean_days": got.get("8a"), "std_tat_median_days": got.get("8b"),
        "exp_tat_mean_hours": got.get("9a"), "exp_tat_median_hours": got.get("9b"),
        "reports_counts": True,
        "service_list_url": svc_url, "service_list_items": svc_items,
        "source_url": meta["url"], "source_sha256": meta["sha256"],
        "extraction_note": "parsed from the CMS numbered-item template by "
                           "pipeline.preauth.extract (numbered)"
                           + ("; " + "; ".join(notes) if notes else ""),
        "needs_review": bool(notes) or None,
    }]
    for dom, hints in DOMAIN_HINTS.items():
        if dom in meta["url"]:
            for k, v in hints.items():
                if not row[0].get(k):
                    row[0][k] = v
    return row


# --------------------------------------------------------------------------
# The bulleted list.
#
# AmeriHealth Caritas and its sister plans publish the disclosure as two
# bulleted lists on an ordinary web page -- "Standard authorizations", then
# "Expedited authorizations" -- one label and one number per line. The rendered
# page also carries the raw HTML of each list as a single long line, so every
# number appears twice; the markup lines are dropped before parsing rather than
# deduplicated afterwards, because first-occurrence-wins would otherwise read
# the standard block's markup as the expedited block's numbers.
#
# These plans count a partial approval as its own outcome, so approved and
# denied do not sum to the total on their own -- approved + denied + partial
# does, exactly, in every document read. The reconciliation check accepts
# either identity and flags anything that satisfies neither.
# --------------------------------------------------------------------------
# A plan with no expedited volume prints one combined list instead of two, and
# carries the expedited counts as their own labelled lines inside it. That block
# is read as "combined" and split back out below.
BULLET_HEAD = re.compile(r"(?i)^(?:(expedited and standard|standard and expedited)|"
                         r"(standard|expedited))\s+authorizations?\s*$")
BULLET_ROW = re.compile(r"(?i)^(.{3,90}?):\*{0,2}\s*([\d,]+(?:\.\d+)?|N/?A)\s*%?\s*$")

BULLET_FIELDS = {
    "total requests received": "total",
    "total requests approved": "approved",
    "total requests denied": "denied",
    # New Hampshire folds partial denials into the denied line and says so in
    # the label; the count means the same thing either way.
    "total requests denied (include partial denials)": "denied",
    "total requests approved other than requested": "partial",
    "total expedited requests received": "exp_total",
    "total expedited requests approved": "exp_approved",
    "total expedited requests denied": "exp_denied",
    "total requests approved after appeal (overturned)": "overturned",
    "average number of days to notification of decision": "mean_days",
    "median number of days to notification of decision": "median_days",
    "average number of hours to notification of decision": "mean_hours",
    "median number of hours to notification of decision": "median_hours",
}


def _bullet_blocks(text: str) -> dict[str, dict[str, str]]:
    blocks: dict[str, dict[str, str]] = {}
    cur = None
    for raw in text.splitlines():
        if "</" in raw or "\\n" in raw or 'id="text-' in raw:
            continue  # the page's own markup, carrying a duplicate of the list
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        head = BULLET_HEAD.match(line)
        if head:
            cur = "combined" if head.group(1) else head.group(2).lower()
            blocks.setdefault(cur, {})
            continue
        if cur is None:
            continue
        row = BULLET_ROW.match(line)
        if not row:
            continue
        label = re.sub(r"\s+", " ", re.sub(r"[^a-z() ]", "", row.group(1).lower())).strip()
        if label in BULLET_FIELDS:
            blocks[cur].setdefault(BULLET_FIELDS[label], row.group(2))
    return blocks


def bulleted_match(meta: dict, text: str) -> bool:
    # The CMS template reads these better where both apply, so defer to it.
    if cms_match(meta, text):
        return False
    blocks = _bullet_blocks(text)
    return "total" in blocks.get("standard", {}) or "total" in blocks.get("combined", {})


def bulleted_parse(meta: dict, text: str) -> list[dict]:
    blocks = _bullet_blocks(text)
    std, exp = blocks.get("standard", {}), blocks.get("expedited", {})

    # One combined list: the printed totals cover both classes, and the
    # expedited part is named on its own lines. Subtracting leaves the standard
    # figures the two-list plans publish directly.
    if not std and "combined" in blocks:
        both = blocks["combined"]
        exp = {f: both[k] for k, f in (("exp_total", "total"), ("exp_approved", "approved"),
                                       ("exp_denied", "denied")) if k in both}
        std = {k: v for k, v in both.items() if not k.startswith("exp_")}
        for field in ("total", "approved", "denied"):
            whole, part = num(std.get(field)), num(exp.get(field))
            if whole is not None and part is not None:
                std[field] = str(whole - part)

    def i(blk, field):
        return num(blk.get(field))

    def f(blk, field):
        return dec((blk.get(field) or "").replace(",", ""))

    clean = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()
             if "</" not in l and "\\n" not in l]
    joined = "\n".join(l for l in clean if l)
    name = re.search(r"(?m)^(.{3,80}?)\s+(20\d\d)\s+Prior Authorization Report", joined) \
        or re.search(r"(?i)^\s*Every year,\s+(.{3,80}?)\s+must provide data", joined, re.M)
    plan = " ".join(name.group(1).split()) if name else None
    year = name.group(2) if (name and name.lastindex and name.lastindex >= 2) else None

    state = None
    for full, code in STATE_CODE.items():
        if plan and re.search(rf"\b{re.escape(full)}\b", plan):
            state = code
            break

    if re.search(r"(?i)\bchip\b", plan or "") or "chip" in meta["url"].lower():
        coverage = "CHIP Managed Care"
    elif re.search(r"(?i)\bmedicare advantage\b", text):
        coverage = "Medicare Advantage"
    elif re.search(r"(?i)\bmedicaid\b", text):
        coverage = "Medicaid Managed Care"
    else:
        coverage = None

    notes = []
    for label, blk in (("standard", std), ("expedited", exp)):
        a, d, p, t = i(blk, "approved"), i(blk, "denied"), i(blk, "partial"), i(blk, "total")
        if None in (a, d, t):
            continue
        # Several of these plans are a handful of requests off their own total
        # -- three in 278,855 at AmeriHealth Caritas Pennsylvania -- which is
        # the payer's rounding, not a misread. A materially different sum is
        # another matter and still goes to review: Keystone First publishes an
        # approved and denied count that together exceed its printed total.
        gap = min(abs(a + d - t), abs(a + d + (p or 0) - t))
        if t and gap / t > 0.005:
            notes.append(f"{label}: approved + denied ({a:,} + {d:,}) does not equal "
                         f"the printed total ({t:,})")

    row = [{
        # left blank so a domain hint can name the parent; these plans trade
        # under brands that do not carry it. Falls back to the plan below.
        "parent_org": None,
        "plan_name": plan,
        "contract_id": None,
        "coverage_type": coverage,
        "state": state,
        "reporting_period": year,
        "std_total": i(std, "total"), "std_approved": i(std, "approved"),
        "std_denied": i(std, "denied"),
        "std_appeals_total": None, "std_appeals_overturned": i(std, "overturned"),
        "ext_review_approved": None,
        "exp_total": i(exp, "total"), "exp_approved": i(exp, "approved"),
        "exp_denied": i(exp, "denied"),
        "std_tat_mean_days": f(std, "mean_days"), "std_tat_median_days": f(std, "median_days"),
        "exp_tat_mean_hours": f(exp, "mean_hours"), "exp_tat_median_hours": f(exp, "median_hours"),
        "reports_counts": True,
        "service_list_url": None, "service_list_items": None,
        "source_url": meta["url"], "source_sha256": meta["sha256"],
        "extraction_note": "parsed from the bulleted standard/expedited web template by "
                           "pipeline.preauth.extract (bulleted)"
                           + ("; " + "; ".join(notes) if notes else ""),
        "needs_review": bool(notes) or None,
    }]
    for dom, hints in DOMAIN_HINTS.items():
        if dom in meta["url"]:
            for k, v in hints.items():
                if not row[0].get(k):
                    row[0][k] = v
    if not row[0]["parent_org"]:
        row[0]["parent_org"] = plan
    return row


# --------------------------------------------------------------------------
# The DentaQuest spreadsheet.
#
# DentaQuest files one workbook per state contract, exported to text sheet by
# sheet. Every metric row is "<label> <count> <denominator> <ratio>", the ratio
# a bare decimal rather than a percentage -- which is why the CMS template, whose
# row pattern ends in a per cent sign, does not see these at all.
#
# The denominator is the class total and is printed on every row, so the totals
# are read from it rather than summed. Turnaround is published in days for both
# classes; the expedited figure is converted to hours to match the schema, and
# the conversion is recorded in the extraction note.
# --------------------------------------------------------------------------
DQ_ROW = re.compile(r"^(.*?[a-z].*?)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+([\d.]+)\s*$")
DQ_TAT = re.compile(r"(?i)^(non-urgent|urgent)\s+prior authorization requests\s*\(.*?\)\s+"
                    r"([\d.]+)\s+days?\s+([\d.]+)\s+days?\s*$")
DQ_CLASS = {"non-urgent prior authorization requests": "std",
            "urgent prior authorization requests": "exp"}


def _dq_sheets(text: str) -> list[list[str]]:
    sheets, cur = [], []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line.startswith("# sheet:"):
            if cur:
                sheets.append(cur)
            cur = []
            continue
        if line:
            cur.append(line)
    if cur:
        sheets.append(cur)
    return [s for s in sheets if any(DQ_ROW.match(l) for l in s)]


def dentaquest_match(meta: dict, text: str) -> bool:
    if cms_match(meta, text):
        return False
    if "Type of decision" not in text:
        return False
    for sheet in _dq_sheets(text):
        seen = {l.lower() for l in sheet}
        if DQ_CLASS.keys() & seen and any(l.lower().startswith("request approved total")
                                          for l in sheet):
            return True
    return False


def dentaquest_parse(meta: dict, text: str) -> list[dict]:
    rows = []
    for sheet in _dq_sheets(text):
        got: dict[str, dict[str, int]] = {"std": {}, "exp": {}}
        tat: dict[str, tuple[float, float]] = {}
        cls = None
        for line in sheet:
            key = line.lower()
            if key in DQ_CLASS:
                cls = DQ_CLASS[key]
                continue
            t = DQ_TAT.match(line)
            if t:
                tat["std" if t.group(1).lower() == "non-urgent" else "exp"] = \
                    (dec(t.group(2)), dec(t.group(3)))
                continue
            m = DQ_ROW.match(line)
            if not m or cls is None:
                continue
            label = re.sub(r"\s+", " ", m.group(1).strip().lower())
            count, denom = num(m.group(2)), num(m.group(3))
            if count is None or denom is None:
                continue
            got[cls].setdefault("total", denom)
            if label == "request approved total":
                got[cls].setdefault("approved", count)
            elif label == "request denied total":
                got[cls].setdefault("denied", count)
            elif label == "request approved only after appeal":
                got[cls].setdefault("overturned", count)
            elif label == "request approved only after time for review was extended":
                got[cls].setdefault("extended", count)
        if "approved" not in got["std"]:
            continue

        body = "\n".join(sheet)
        client = re.search(r"(?i)^Client Name:\s*(.+)$", body, re.M)
        lob = re.search(r"(?i)^Line of Business:\s*(.+)$", body, re.M)
        period = re.search(r"(?i)^Reporting Period:\s*(.+)$", body, re.M)
        plan = " ".join(client.group(1).split()) if client else None

        state = None
        slug = re.search(r"/([a-z]{2})-[a-z0-9-]+\.(?:xlsx?|csv|pdf)", meta["url"], re.I)
        if slug and slug.group(1).upper() in set(STATE_CODE.values()):
            state = slug.group(1).upper()
        if state is None:
            for full, code in STATE_CODE.items():
                if plan and re.search(rf"\b{re.escape(full)}\b", plan):
                    state = code
                    break

        line_of_business = (lob.group(1).strip().lower() if lob else "")
        if "chip" in line_of_business:
            coverage = "CHIP Managed Care"
        elif "medicare" in line_of_business:
            coverage = "Medicare Advantage"
        elif "medicaid" in line_of_business:
            coverage = "Medicaid Managed Care"
        else:
            coverage = None

        notes = []
        for label, cls in (("standard", "std"), ("expedited", "exp")):
            a, d, t = got[cls].get("approved"), got[cls].get("denied"), got[cls].get("total")
            if None in (a, d, t):
                continue
            if a + d != t:
                notes.append(f"{label}: approved + denied ({a:,} + {d:,}) does not equal "
                             f"the printed total ({t:,})")

        exp_tat = tat.get("exp")
        rows.append({
            "parent_org": "DentaQuest",
            "plan_name": plan,
            "contract_id": None,
            "coverage_type": coverage,
            "state": state,
            "reporting_period": period.group(1).strip() if period else None,
            "std_total": got["std"].get("total"), "std_approved": got["std"].get("approved"),
            "std_denied": got["std"].get("denied"),
            "std_appeals_total": None,
            "std_appeals_overturned": got["std"].get("overturned"),
            "ext_review_approved": got["std"].get("extended"),
            "exp_total": got["exp"].get("total"), "exp_approved": got["exp"].get("approved"),
            "exp_denied": got["exp"].get("denied"),
            "std_tat_mean_days": tat.get("std", (None, None))[0],
            "std_tat_median_days": tat.get("std", (None, None))[1],
            "exp_tat_mean_hours": round(exp_tat[0] * 24, 2) if exp_tat and exp_tat[0] is not None else None,
            "exp_tat_median_hours": round(exp_tat[1] * 24, 2) if exp_tat and exp_tat[1] is not None else None,
            "reports_counts": True,
            "service_list_url": None, "service_list_items": None,
            "source_url": meta["url"], "source_sha256": meta["sha256"],
            "extraction_note": "parsed from the DentaQuest workbook export by "
                               "pipeline.preauth.extract (dentaquest); expedited turnaround "
                               "converted from days as published to hours"
                               + ("; " + "; ".join(notes) if notes else ""),
            "needs_review": bool(notes) or None,
        })
    return rows


# --------------------------------------------------------------------------
# The Elevance scorecard.
#
# Anthem, Anthem Blue Cross and Wellpoint publish one PDF per state built from
# the same reporting tool: a header line naming the period, the line of business
# and the market state, then the rule's numbered items. It is the numbered
# template with a header and one difference that matters -- most states publish
# only the percentages, and item 2a/2b (the counts) appear in some states and
# not others. Percentages alone are still a filing; the counts stay NULL and
# reports_counts says which it is.
#
# Both language editions are read here. The standing rule is to skip a
# translation, because it is the same disclosure twice -- but Indiana publishes
# the Spanish edition and nothing else (the English URL 404s), and those four
# documents carry counts. So a translation is skipped only when its English
# twin is actually in the store.
# --------------------------------------------------------------------------
SCORE_HEAD = re.compile(r"(?i)(Scorecard:|Ficha de puntuaci)")
SCORE_FIELDS = {
    "period": (r"Scorecard:\s*(.+?),\s*LOB", r"Ficha de puntuaci\S*n:\s*(?:del\s+)?(.+?)\.\s"),
    "lob": (r"LOB:\s*(.+?),\s*Plan Name", r"L\S*nea de negocios \(LOB\):\s*(.+?)\.\s"),
    "plan": (r"Plan Name:\s*(.+?),\s*Market", r"Nombre del plan:\s*(.+?)\.\s"),
    "state": (r"States:\s*([A-Z]{2})\b", r"Estados de comercializaci\S*n:\s*([A-Z]{2})\b"),
    "issuer": (r"Issuer Name:\s*(.+?)(?:,|\.|$)", r"Nombre del emisor:\s*(.+?)(?:\.|$)"),
}
SCORE_ORG = {"wellpoint.com": "Elevance Health", "anthembluecross.com": "Elevance Health",
             "anthem.com": "Elevance Health"}

_STORED_URLS: set[str] | None = None


def _stored_urls() -> set[str]:
    global _STORED_URLS
    if _STORED_URLS is None:
        # Only documents that actually rendered count as present. A failed
        # fetch leaves a SOURCE.json behind, and treating that as the English
        # edition would silence the Spanish one -- which is the only edition
        # Indiana published.
        urls = set()
        for p in DOCS.glob("*/SOURCE.json"):
            try:
                meta = json.loads(p.read_text())
            except (ValueError, OSError):
                continue
            if meta.get("error") or not (p.parent / "text.txt").exists():
                continue
            if "url" in meta:
                urls.add(meta["url"])
        _STORED_URLS = urls
    return _STORED_URLS


def _is_spanish(text: str) -> bool:
    return bool(re.search(r"(?i)Ficha de puntuaci|autorizaci\S*n previa", text[:2000]))


def scorecard_match(meta: dict, text: str) -> bool:
    # The lettered numbered template reads these better where it applies.
    if numbered_match(meta, text):
        return False
    if not SCORE_HEAD.search(text[:2000]):
        return False
    if _is_spanish(text):
        # The English edition is published either as the bare name or with an
        # _EN suffix, depending on the state.
        twins = {re.sub(r"(?i)_sp\.pdf$", suffix, meta["url"]) for suffix in (".pdf", "_EN.pdf")}
        if (twins - {meta["url"]}) & _stored_urls():
            return False
    keys = {m.group(1) + m.group(2) for m in ITEM.finditer(text)}
    return {"2", "3"} <= keys


def scorecard_parse(meta: dict, text: str) -> list[dict]:
    got: dict[str, float] = {}
    for m in ITEM.finditer(text):
        key = m.group(1) + m.group(2)
        val = dec(m.group(4).replace(",", ""))
        if val is not None:
            got.setdefault(key, val)

    def i(k):
        v = got.get(k)
        return int(v) if v is not None else None

    es = _is_spanish(text)
    # The header wraps mid-phrase in the PDFs -- "Nombre / del plan: HCC" --
    # so it is read as one line or the plan name is lost.
    header = " ".join(text[:1400].split())
    head = {}
    for field, (en_pat, es_pat) in SCORE_FIELDS.items():
        m = re.search(es_pat if es else en_pat, header)
        head[field] = " ".join(m.group(1).split()) if m else None

    if head["period"] and es:
        head["period"] = re.sub(r"\s+al\s+", " to ", head["period"])

    plan = head["plan"]
    if plan and plan.strip().lower() in ("all", "todos", "todas"):
        plan = None
    issuer = head["issuer"]
    if issuer and issuer.strip().lower() in ("all", "todos", "todas"):
        issuer = None

    lob = (head["lob"] or "").lower()
    if "medicaid" in lob:
        coverage = "Medicaid Managed Care"
    elif "chip" in lob:
        coverage = "CHIP Managed Care"
    elif "medicare" in lob:
        coverage = "Medicare Advantage"
    else:
        coverage = None

    org = next((v for k, v in SCORE_ORG.items() if k in meta["url"]), None)
    std_total = i("2b") or i("3b")

    notes = []
    if None not in (i("2a"), i("3a"), std_total) and i("2a") + i("3a") != std_total:
        notes.append(f"standard: approved + denied ({i('2a'):,} + {i('3a'):,}) does not equal "
                     f"the printed total ({std_total:,})")
    if es:
        notes.append("read from the Spanish edition; the English URL returns 404")

    return [{
        "parent_org": org,
        "plan_name": plan or issuer or org,
        "contract_id": None,
        "coverage_type": coverage,
        "state": head["state"],
        "reporting_period": head["period"],
        "std_total": std_total, "std_approved": i("2a"), "std_denied": i("3a"),
        "std_appeals_total": i("4b"), "std_appeals_overturned": i("4a"),
        "ext_review_approved": i("5a"),
        "exp_total": i("6b") or i("7b"), "exp_approved": i("6a"), "exp_denied": i("7a"),
        "std_denied_pct": got.get("3"), "exp_denied_pct": got.get("7"),
        "std_tat_mean_days": got.get("8a"), "std_tat_median_days": got.get("8b"),
        "exp_tat_mean_hours": None, "exp_tat_median_hours": None,
        "reports_counts": i("2a") is not None or i("6a") is not None,
        "service_list_url": None, "service_list_items": None,
        "source_url": meta["url"], "source_sha256": meta["sha256"],
        "extraction_note": "parsed from the Elevance scorecard template by "
                           "pipeline.preauth.extract (scorecard)"
                           + ("; " + "; ".join(notes) if notes else ""),
        # A Spanish-only edition is a note, not a defect; only a failed
        # reconciliation holds the row back.
        "needs_review": bool([n for n in notes if "does not equal" in n]) or None,
    }]


# --------------------------------------------------------------------------
# Sectioned totals.
#
# The commonest shape after the CMS table: a heading that names the request
# class, then the counts as labelled lines under it. Blue Shield of California
# prints "Total requests / Total approved / Total denied", Alliance Health puts
# the class total in a third column, and Community Health Plan of Washington
# puts the label on one line and "989 /1973" on the next. One state machine
# reads all three, because the only thing that varies is where the number sits
# relative to its label.
#
# A section heading only ever changes which class the following numbers belong
# to; the first value wins within a section, so the appeal tables that repeat
# the words "approved" and "denied" further down cannot overwrite the counts.
# --------------------------------------------------------------------------
SEC_STD = re.compile(r"(?i)\b(?<!non-)(?<!non )(standard|non-urgent)\b")
SEC_EXP = re.compile(r"(?i)\b(?<!non-)(?<!non )(expedited|urgent)\b")
SEC_CTX = re.compile(r"(?i)(prior auth|pre-approval|authorization request)")
SEC_PAIR = re.compile(r"^([\d,]+)\s*/\s*([\d,]+)$")
SEC_LONE = re.compile(r"^([\d,]+(?:\.\d+)?)\s*(hrs|hours|days?)?$", re.I)
SEC_ROW = re.compile(r"^([A-Za-z][A-Za-z ()%,'-]{2,60}?)\s+([\d,]+)"
                     r"(?:\s+([\d,]+))?(?:\s+([\d.]+)\s*%)?\s*$")
SEC_FIELDS = {
    "total requests": "total", "total": "total",
    "total approved": "approved", "total requests approved": "approved",
    "approved": "approved",
    "total denied": "denied", "total requests denied": "denied", "denied": "denied",
    "extension total approved": "extended", "approved after timeframe was extended": "extended",
    "total appeals": "appeals_total",
    "requests approved only afer appeal": "overturned",
    "requests approved only after appeal": "overturned",
    "approved after appeal": "overturned",
    "response time average": "tat_mean", "response time median": "tat_median",
}


def _sec_label(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", raw.lower())).strip()
    return SEC_FIELDS.get(key)


def _sec_blocks(text: str) -> dict[str, dict[str, str]]:
    blocks: dict[str, dict[str, str]] = {"std": {}, "exp": {}}
    cls, appeal, pending = None, False, None
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        bare = line.rstrip(":")
        # A heading names a request class and carries no count of its own --
        # but it may carry the deadline it is quoting ("within 72 hours"), so
        # what disqualifies it is looking like a labelled row, not any digit.
        if len(bare) < 110 and not SEC_ROW.match(bare):
            named = SEC_CTX.search(bare) and (SEC_STD.search(bare) or SEC_EXP.search(bare))
            if named:
                cls = "std" if SEC_STD.search(bare) else "exp"
                appeal = bool(re.search(r"(?i)appeal", bare))
                pending = None
                continue
        if not re.search(r"\d", bare) and len(bare) < 90:
            if re.fullmatch(r"(?i)appeals?", bare):
                appeal, pending = True, None
                continue
            field = _sec_label(bare)
            if field:
                pending = field
                continue
            pending = None
            continue
        if cls is None:
            continue
        blk = blocks[cls]
        pair = SEC_PAIR.match(line)
        if pair and pending:
            blk.setdefault(pending, pair.group(1))
            blk.setdefault("appeals_total" if pending == "overturned" else pending + "_of",
                           pair.group(2))
            pending = None
            continue
        lone = SEC_LONE.match(line)
        if lone and pending:
            blk.setdefault(pending, lone.group(1))
            pending = None
            continue
        row = SEC_ROW.match(line)
        if not row:
            continue
        field = _sec_label(row.group(1))
        if not field:
            continue
        if appeal:
            # In an appeal table "approved" means overturned on appeal, and the
            # third column is the number of appeals rather than the class total.
            if field in ("approved", "overturned"):
                blk.setdefault("overturned", row.group(2))
                if row.group(3):
                    blk.setdefault("appeals_total", row.group(3))
            elif field == "appeals_total":
                blk.setdefault("appeals_total", row.group(2))
            continue
        blk.setdefault(field, row.group(2))
        if row.group(3):
            blk.setdefault("total", row.group(3))
    return blocks


def sectioned_match(meta: dict, text: str) -> bool:
    for name, (m, _, _) in TEMPLATES.items():
        if name != "sectioned" and m(meta, text):
            return False
    b = _sec_blocks(text)
    return all(k in b["std"] for k in ("approved", "denied")) \
        and all(k in b["exp"] for k in ("approved", "denied"))


def sectioned_parse(meta: dict, text: str) -> list[dict]:
    b = _sec_blocks(text)
    std, exp = b["std"], b["exp"]

    def i(blk, field):
        return num(blk.get(field))

    def f(blk, field):
        return dec((blk.get(field) or "").replace(",", ""))

    notes = []
    totals = {}
    for label, blk in (("standard", std), ("expedited", exp)):
        a, d, t = i(blk, "approved"), i(blk, "denied"), i(blk, "total")
        if t is None and None not in (a, d):
            # Not every plan prints the class total; where it is absent it is
            # the sum, and saying so is better than leaving the column empty.
            t = a + d
            notes.append(f"{label} total not printed; taken as approved + denied")
        elif None not in (a, d, t) and t and abs(a + d - t) / t > 0.005:
            notes.append(f"{label}: approved + denied ({a:,} + {d:,}) does not equal "
                         f"the printed total ({t:,})")
        totals[label] = t

    row = [{
        "parent_org": None, "plan_name": None, "contract_id": None,
        "coverage_type": None, "state": None, "reporting_period": None,
        "std_total": totals["standard"], "std_approved": i(std, "approved"),
        "std_denied": i(std, "denied"),
        "std_appeals_total": i(std, "appeals_total"),
        "std_appeals_overturned": i(std, "overturned"),
        "ext_review_approved": i(std, "extended"),
        "exp_total": totals["expedited"], "exp_approved": i(exp, "approved"),
        "exp_denied": i(exp, "denied"),
        "std_tat_mean_days": f(std, "tat_mean"), "std_tat_median_days": f(std, "tat_median"),
        "exp_tat_mean_hours": f(exp, "tat_mean"), "exp_tat_median_hours": f(exp, "tat_median"),
        "reports_counts": True,
        "service_list_url": None, "service_list_items": None,
        "source_url": meta["url"], "source_sha256": meta["sha256"],
        "extraction_note": "parsed from a sectioned totals layout by "
                           "pipeline.preauth.extract (sectioned)"
                           + ("; " + "; ".join(notes) if notes else ""),
        "needs_review": bool([n for n in notes if "does not equal" in n]) or None,
    }]
    for dom, hints in DOMAIN_HINTS.items():
        if dom in meta["url"]:
            for k, v in hints.items():
                if not row[0].get(k):
                    row[0][k] = v
    if not row[0]["plan_name"]:
        # A plan filing on its state's website is named by the file, not the
        # domain: ".../TrueCare-2025-Interoperability-Prior-Auth.pdf".
        stem = re.search(r"/([A-Za-z][A-Za-z-]{2,40}?)[-_](?:20\d\d|CY)", meta["url"])
        if stem:
            row[0]["plan_name"] = stem.group(1).replace("-", " ")
    return row


# --------------------------------------------------------------------------
# WellSense, read from the picture.
#
# WellSense publishes the report as an image-only PDF -- no text layer, so it
# was in the store for weeks contributing nothing. tools/ocr_scanned.py reads it
# with the OCR built into macOS; this parses what comes back.
#
# The two request classes are printed side by side, so a row of the page reads
# "Total requests 49,926 Total requests 2,113": the same label twice, non-urgent
# on the left and urgent on the right. First occurrence is standard, second is
# expedited. A label is only believed when a number follows it immediately,
# which is what keeps the percentage rows ("Requests approved percentage (%)
# 88%") and any OCR debris from being read as counts.
#
# Every one of these rows reconciles against the payer's own printed total, in
# both classes, in all five documents. That is the check that makes an OCR'd
# figure publishable: a misread digit would break it.
# --------------------------------------------------------------------------
WS_FIELDS = [
    ("requests approved only after appeal", "overturned"),
    ("total appealed requests", "appeals_total"),
    ("total requests", "total"),
    ("requests approved", "approved"),
    ("requests denied", "denied"),
]
WS_ROW = re.compile(r"(?i)(" + "|".join(re.escape(k) for k, _ in WS_FIELDS)
                    + r")\s+([\d][\d,]*)(?!\s*%)")
WS_TAT = re.compile(r"(?i)^(non-urgent|urgent) prior authorization requests\s*\([^)]*\)\s+"
                    r"([\d.]+)\s+([\d.]+)")


def wellsense_match(meta: dict, text: str) -> bool:
    return "CMS Prior Authorization Decisions" in text \
        and re.search(r"(?i)non-urgent prior authorization requests", text) is not None \
        and len(WS_ROW.findall(text)) >= 4


def wellsense_parse(meta: dict, text: str) -> list[dict]:
    lookup = dict(WS_FIELDS)
    std: dict[str, str] = {}
    exp: dict[str, str] = {}
    for line in text.splitlines():
        found: dict[str, list[str]] = {}
        for label, value in WS_ROW.findall(line):
            found.setdefault(lookup[label.lower()], []).append(value)
        for field, values in found.items():
            # Both columns print the label, so a row carries it exactly twice.
            # Where OCR dropped one of the two numbers only one match survives,
            # and there is no way to tell which column it came from -- so it is
            # left out rather than guessed onto the standard side.
            if len(values) == 2:
                std.setdefault(field, values[0])
                exp.setdefault(field, values[1])

    tat = {}
    for line in text.splitlines():
        m = WS_TAT.match(re.sub(r"\s+", " ", line).strip())
        if m:
            tat.setdefault("std" if m.group(1).lower() == "non-urgent" else "exp",
                           (dec(m.group(2)), dec(m.group(3))))

    lob = re.search(r"(?i)^Line of Business:\s*(.+)$", text, re.M)
    plan = " ".join(lob.group(1).split()) if lob else None
    period = re.search(r"(?i)^Reporting period:\s*(.+)$", text, re.M)

    low = (plan or "").lower()
    if "medicare" in low:
        coverage = "Medicare Advantage"
    elif "medicaid" in low or "masshealth" in low:
        coverage = "Medicaid Managed Care"
    elif "clarity" in low:
        coverage = "Marketplace QHP"
    else:
        coverage = None

    state = None
    for full, code in STATE_CODE.items():
        if plan and full.lower() in low:
            state = code
            break
    if state is None and "masshealth" in low:
        state = "MA"

    def i(blk, field):
        return num(blk.get(field))

    notes = ["read by OCR from an image-only PDF (tools/ocr_scanned.py); "
             "both classes reconcile against the printed totals"]
    for label, blk in (("standard", std), ("expedited", exp)):
        a, d, t = i(blk, "approved"), i(blk, "denied"), i(blk, "total")
        if None in (a, d, t):
            notes.append(f"{label}: counts incomplete")
        elif a + d != t:
            notes.append(f"{label}: approved + denied ({a:,} + {d:,}) does not equal "
                         f"the printed total ({t:,})")

    exp_tat = tat.get("exp")
    return [{
        "parent_org": "WellSense Health Plan",
        "plan_name": f"WellSense {plan}" if plan else "WellSense Health Plan",
        "contract_id": None,
        "coverage_type": coverage,
        "state": state,
        "reporting_period": period.group(1).strip() if period else None,
        "std_total": i(std, "total"), "std_approved": i(std, "approved"),
        "std_denied": i(std, "denied"),
        "std_appeals_total": i(std, "appeals_total"),
        "std_appeals_overturned": i(std, "overturned"),
        "ext_review_approved": None,
        "exp_total": i(exp, "total"), "exp_approved": i(exp, "approved"),
        "exp_denied": i(exp, "denied"),
        "std_tat_mean_days": tat.get("std", (None, None))[0],
        "std_tat_median_days": tat.get("std", (None, None))[1],
        # Published in days for both classes; the schema keeps the expedited
        # figure in hours.
        "exp_tat_mean_hours": round(exp_tat[0] * 24, 2) if exp_tat and exp_tat[0] is not None else None,
        "exp_tat_median_hours": round(exp_tat[1] * 24, 2) if exp_tat and exp_tat[1] is not None else None,
        "reports_counts": True,
        "service_list_url": None, "service_list_items": None,
        "source_url": meta["url"], "source_sha256": meta["sha256"],
        "extraction_note": "parsed from the WellSense report by "
                           "pipeline.preauth.extract (wellsense); " + "; ".join(notes),
        "needs_review": bool([n for n in notes if "does not equal" in n or "incomplete" in n]) or None,
    }]


TEMPLATES = {
    "humana": (humana_match, humana_parse, "seg_w5_humana.json"),
    "kaiser": (kaiser_match, kaiser_parse, "seg_w5_kaiser.json"),
    "anthem": (anthem_match, anthem_parse, "seg_w5_anthem.json"),
    "numbered": (numbered_match, numbered_parse, "seg_w5_numbered.json"),
    "bulleted": (bulleted_match, bulleted_parse, "seg_w5_bulleted.json"),
    "dentaquest": (dentaquest_match, dentaquest_parse, "seg_w5_dentaquest.json"),
    "scorecard": (scorecard_match, scorecard_parse, "seg_w5_scorecard.json"),
    "sectioned": (sectioned_match, sectioned_parse, "seg_w5_sectioned.json"),
    "wellsense": (wellsense_match, wellsense_parse, "seg_w5_wellsense.json"),
    "cms": (cms_match, cms_parse, "seg_w5_cms_template.json"),
}


# "cms" is the fallback: it reads the CMS template wherever it appears, which
# includes documents a payer-specific template already handles better (a specific
# parser knows the payer's own name for its plans and which market they are in).
# So the general template defers to every specific one.
GENERAL = "cms"


def run(name: str) -> list[dict]:
    matcher, parser, _ = TEMPLATES[name]
    claimed = set()
    if name == GENERAL:
        for other, (m2, _, _) in TEMPLATES.items():
            if other == GENERAL:
                continue
            claimed |= {meta["url"] for meta, text in stored() if m2(meta, text)}
    rows = []
    for meta, text in stored():
        if meta["url"] in claimed:
            continue
        if matcher(meta, text):
            rows.extend(parser(meta, text))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("template", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docs = stored()
    if args.list or not args.template:
        print(f"{len(docs)} readable documents in the store")
        for name, (matcher, _, out) in TEMPLATES.items():
            n = sum(1 for m, t in docs if matcher(m, t))
            print(f"  {name:<12} {n:>4} matching documents -> data/{out}")
        return 0

    if args.template not in TEMPLATES:
        raise SystemExit(f"unknown template: {args.template}")
    rows = run(args.template)
    out = DATA / TEMPLATES[args.template][2]
    # A row whose own counts do not reconcile is not published. It goes to a
    # review file instead, because a number that fails the payer's own printed
    # total is a parse to fix or a document to read by hand -- not a filing.
    flagged = [r for r in rows if r.get("needs_review")]
    rows = [r for r in rows if not r.get("needs_review")]
    if flagged:
        rp = ROOT / "out" / "preauth" / "needs_review.json"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(flagged, indent=1) + "\n")
        print(f"{len(flagged)} rows held back for review -> {rp.relative_to(ROOT)}")
    print(f"{len(rows)} filings parsed")
    for r in rows[:6]:
        print(f"  {str(r['contract_id'] or r['state']):<8} std {str(r['std_total']):>10}"
              f"  denied {str(r['std_denied']):>9}  exp {str(r['exp_total']):>9}")
    if args.dry_run:
        return 0
    out.write_text(json.dumps({"filings": rows}, indent=1) + "\n")
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
