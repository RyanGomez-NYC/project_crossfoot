"""
Classify the recorded coverage gaps and write out/coverage_gaps.csv.

Why this exists: a gap is not one thing, and treating it as one overstates what
the dataset can claim. Three groups fall out of the recorded reasons:

  blocked    The document exists and could not be read — robots.txt, HTTP 403,
             bot protection, scans, binaries, client-rendered pages. A finding
             about the payer. Only this group supports the claim that a filing
             exists and is being withheld from machines.
  missing    A search that came up empty — a guessed URL that 404'd, a retired
             plan site, a payer never located. A finding about this crawl, not
             about the payer.
  not_a_gap  Not a gap at all — a duplicate of a document already captured, or a
             plan with no CY2025 filing to find because it did not operate in
             2025 or publishes under a different rule. Counting these as gaps
             inflates the gap total.

A reason may only be classified `blocked` if it carries direct evidence that a
document exists and something stopped the crawler from reading it — a robots.txt
rule, a 403, a bot wall, a scan, a binary, a client-rendered page, a truncated
read. Absent that evidence, prose like "contains no metrics" describes a search
that came up empty, not a payer withholding anything, and belongs in `missing`.
See the note above `HARD_BLOCK` for what forced that rule.

One `missing` kind is worth naming separately: `tool_limited` is a fetch that
never completed — a rate limit, an exhausted search budget. It is neither a
finding about the payer nor really about the crawl's reach; it is a record that
the question was never asked. Counting it as either would be a lie by omission.

Splitting them here, in code, means the split is reproducible and every consumer
gets the same answer. This module is the single classifier; the website imports
`classify` rather than re-deriving the categories from the reason text itself.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "out"

# Ordered: the first pattern that matches wins, so a document that is both
# behind robots.txt and a scan is filed under the block that stopped it first.
GAP_KINDS: list[tuple[str, str, str, str]] = [
    # (kind, group, pattern, what it means)
    #
    # not_a_gap first: an entry that was never a gap must not be counted as one
    # just because its note also mentions a 404 it passed through on the way.
    ("duplicate", "not_a_gap", r"duplicate|already[- ]captured|already captured|captured separately|captured elsewhere",
     "The same filing was captured from another document; this one was skipped deliberately."),
    ("no_cy2025_filing", "not_a_gap", r"not available|launched on january 1, 2026|no marketplace filing published|"
                                      r"only;? no |quarterly pdfs|rule 115|requirements only",
     "No CY2025 CMS-0057-F filing exists to find — the plan did not operate in 2025, "
     "or publishes under a different rule."),

    ("robots",   "blocked", r"robots",
     "Reachable, but the host disallows crawling that path."),
    ("http_403", "blocked", r"\b403\b|forbidden",
     "The server refuses the request on every scheme and host variant tried."),
    ("bot_protection", "blocked", r"imperva|incapsula|cloudflare|bot protection|captcha|ssl verification",
     "A bot-protection layer stands between the crawler and a document that exists."),
    ("image_only", "blocked", r"scanned|image-only|image only|image-embedded|embedded image|\bpng\b|slide",
     "Published as pictures of pages; no number is extractable without OCR."),
    ("binary",   "blocked", r"\.xlsx|\bbinary\b|workbook",
     "Published in a binary office format the crawler does not open."),
    ("client_rendered", "blocked", r"client-side|client side|javascript|renders? client",
     "The page assembles in the browser and serves no static document."),
    ("partial_read", "blocked", r"\bpartial\b|truncat",
     "A long consolidated document whose later contracts were not transcribed."),
    ("no_filing_on_page", "blocked", r"no metrics|no cms|contains no|no prior auth|no reporting|placeholder|not present",
     "The disclosure page loads and carries no metrics report."),

    ("http_404", "missing", r"\b404\b",
     "A path guessed from a sibling brand's pattern that does not exist here."),
    ("redirect", "missing", r"\b30[12]\b|redirect|retired|no longer serves",
     "The plan site is gone or folded into a corporate domain; no filing found at either."),
    ("not_located", "missing",
     r"not located|no primary|could not locate|not find|searched|does not exist|did not exist|"
     r"\bno\b[^.]{0,40}\bfiling\b[^.]{0,20}(found|located)|budget",
     "In scope, no filing found. It may be published somewhere not yet reached."),
]

# Two more kinds, added when the third collection wave quadrupled the number of
# recorded reasons and exposed an ordering bug in the table above.
#
# The table is first-match-wins, and the `blocked` patterns sit above the
# `missing` ones. That was harmless while the reasons were written by a handful
# of collectors in a uniform voice. It stopped being harmless once a reason like
# "Searched the provider pages; contains no prior auth metrics. Not located."
# started appearing, because "contains no" fires `no_filing_on_page` — a
# `blocked` kind, i.e. a finding about the payer — for what is plainly a search
# that came up empty, i.e. a finding about this crawl. Left alone that inflated
# `blocked` from 39 to 194 and would have restated, at four times the scale, the
# exact overstatement this module was written to correct.
#
# So `classify` no longer runs the table straight through. It first asks whether
# the reason carries direct evidence that a document exists and could not be
# read. Only then may a `blocked` kind win.
GAP_KINDS += [
    ("tool_limited", "missing",
     r"\b429\b|rate[- ]limit|search budget|budget exhausted|search quota|"
     r"no working search engine|websearch (?:budget|quota)",
     "The fetch never completed — rate limit or exhausted search budget. This says "
     "nothing about the payer and nothing about whether the document exists."),
]

# Direct evidence that a document exists and something stopped the crawler from
# reading it. Without one of these, a reason cannot be classified `blocked`.
HARD_BLOCK = re.compile(
    r"robots\.txt disallow|blocked by robots|robots-blocked|"
    r"\b403\b|forbidden|imperva|incapsula|captcha|"
    r"scanned|image[- ]only|chart/graphic|"
    r"\.xlsx|workbook|\bbinary\b|"
    r"client[- ]side|renders? client|"
    r"truncat|\bpartial\b",
    re.I,
)

# The language of a search that came up empty.
CRAWL_MISS = re.compile(
    r"not located|could not locate|could not confirm|no .{0,40}located|"
    r"\bsearched\b|\bguessed\b|does not resolve",
    re.I,
)

FALLBACK = ("other", "missing",
            "Redirects, quarterly-only publication, and disclosure links that resolve to nothing usable.")

_COMPILED = [(k, g, re.compile(p, re.I), d) for k, g, p, d in GAP_KINDS]
_NOT_A_GAP = [(k, g, p, d) for k, g, p, d in _COMPILED if g == "not_a_gap"]
_TOOL_LIMITED = next((k, g, p, d) for k, g, p, d in _COMPILED if k == "tool_limited")


def classify(reason: str) -> tuple[str, str]:
    """Return (gap_kind, gap_group) for one recorded reason."""
    reason = reason or ""

    # An entry that was never a gap must not be counted as one, whatever else
    # its note mentions. Unchanged from the original ordering.
    for kind, group, pattern, _ in _NOT_A_GAP:
        if pattern.search(reason):
            return kind, group

    if not HARD_BLOCK.search(reason):
        # Nothing here says a document exists and resisted reading, so no
        # `blocked` kind may claim it.
        kind, group, pattern, _ = _TOOL_LIMITED
        if pattern.search(reason):
            return kind, group
        if CRAWL_MISS.search(reason):
            return "not_located", "missing"

    for kind, group, pattern, _ in _COMPILED:
        if pattern.search(reason):
            return kind, group
    return FALLBACK[0], FALLBACK[1]


def load() -> list[dict]:
    path = DATA / "merged_failures.json"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run merge.py first.")
    rows = []
    for g in json.loads(path.read_text()):
        kind, group = classify(g.get("reason", ""))
        rows.append({
            "segment":   g.get("_segment") or "",
            "gap_kind":  kind,
            "gap_group": group,
            "url":       g.get("url", ""),
            "reason":    g.get("reason", ""),
        })
    rows.sort(key=lambda r: (r["gap_group"], r["gap_kind"], r["segment"], r["url"]))
    return rows


def main() -> None:
    rows = load()
    OUT.mkdir(exist_ok=True)
    # csv.writer's own lineterminator defaults to CRLF regardless of how the file
    # was opened, which would leave this the only CRLF file in the repo.
    with (OUT / "coverage_gaps.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["segment", "gap_kind", "gap_group", "url", "reason"],
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    counts = {"blocked": 0, "missing": 0, "not_a_gap": 0}
    for r in rows:
        counts[r["gap_group"]] += 1
    print(f"{len(rows)} recorded entries -> out/coverage_gaps.csv")
    print(f"  {counts['blocked']:4} confirmed to exist, could not be read")
    print(f"  {counts['missing']:4} searched for, not found")
    print(f"  {counts['not_a_gap']:4} not a coverage gap (duplicate, or no CY2025 filing exists)")
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["gap_kind"]] = by_kind.get(r["gap_kind"], 0) + 1
    for kind, group, _, _ in GAP_KINDS + [(FALLBACK[0], FALLBACK[1], "", "")]:
        if by_kind.get(kind):
            print(f"       {by_kind[kind]:4}  {kind:20} ({group})")


if __name__ == "__main__":
    main()
