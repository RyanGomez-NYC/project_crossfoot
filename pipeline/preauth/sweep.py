"""
Sweep the publishers we already know for the documents we do not have.

The expensive way to grow this dataset is to go looking for payers one at a
time. The cheap way is to notice that we already hold 637 filings from 136
publisher domains, and that a publisher which posted one disclosure almost
always posted many -- Humana put 42 on one CDN, Wellcare 33 across its state
provider pages. Those domains are confirmed, so nothing here is a guess about
where a payer publishes; it is a search of somewhere we know they do.

The whole run is one command with one summary at the end. It crawls, filters,
fetches, parses and reports without anything in the middle needing to be read:

    python3 -m pipeline.preauth.sweep --domains /tmp/known_domains.txt

  crawl    breadth-first from each domain root, depth 2, only into sections that
           could plausibly hold a disclosure. Document links are kept from any
           host, because payers serve PDFs off CDNs that carry no links of their
           own and would otherwise be invisible.
  filter   a candidate must look like a disclosure in its URL or its link text.
  fetch    through docs.py, so every document keeps its bytes and its SHA-256.
  parse    through extract.py's CMS-template parser. Rows that fail the payer's
           own printed total are held back, not published.

robots.txt is obeyed throughout; a disallowed path is recorded as blocked rather
than fetched, because a payer hiding a mandated disclosure from crawlers is a
finding this project exists to publish.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import extract
from .discover import blocked, robots
from .docs import fetch, grab, links

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "preauth"
DATA = ROOT / "data"

# Sections of a payer site where a disclosure could live. Crawling anything else
# (plan finders, news archives, provider directories) costs hundreds of requests
# and returns nothing.
SECTION = re.compile(
    r"provider|legal|about|resource|complian|transparen|discl|prior|auth|metric|"
    r"interoper|medicare|medicaid|marketplace|member|cms", re.I)

# A candidate document: says prior authorization somewhere, and says metrics or
# report somewhere. Both halves are needed -- "prior authorization" alone
# matches every requirements list and code lookup tool on the site.
SIG_A = re.compile(r"prior.?auth|preauth|pre-auth|0057|interoperab", re.I)
SIG_B = re.compile(r"metric|report|summary|disclosur|statistic", re.I)
DOC_EXT = (".pdf", ".xlsx", ".xls", ".ashx", ".csv")

SKIP = re.compile(r"\.(jpg|jpeg|png|gif|svg|css|js|woff2?|ico|mp4|zip)($|\?)", re.I)


def host_of(url: str) -> str:
    """
    Registrable host, with "www." stripped.

    Not cosmetic: robots.txt is fetched for "anthem.com" but its documents are
    served from "www.anthem.com", and comparing the two raw made every rule look
    like it belonged to a different site. The effect was that disallowed paths
    were treated as third-party links and fetched anyway.
    """
    return re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc.lower())


# Sitemap entries worth opening. A payer's authorization pages are the ones that
# carry the disclosure links, and the sitemap names them; crawling in from the
# home page instead means two hops of marketing before anything relevant, which
# is how the first version of this returned nothing.
HUB = re.compile(r"authoriz|prior.?auth|preauth|interoper|metric|cms|complian|"
                 r"transparen|discl|legal", re.I)


def hub_pages(domain: str, cap: int) -> list[str]:
    """Pages on this domain that could carry disclosure links, from its sitemap."""
    from .discover import sitemap_urls
    sitemaps, _ = robots(domain)
    if not sitemaps:
        sitemaps = [f"https://{domain}/sitemap.xml"]
    seen: set[str] = set()
    urls: list[str] = []
    for sm in sitemaps[:6]:
        for u in sitemap_urls(sm, seen):
            if HUB.search(u):
                urls.append(u)
    # Rank rather than sort by length. The word in the URL says how likely the
    # page is to carry the disclosure itself: "metrics" almost always does,
    # "authorizations" often does, "legal" rarely but cheaply. Sorting by path
    # length instead picks up section indexes that link to nothing, which is how
    # a domain with forty perfectly good authorization pages returned nothing.
    def rank(u: str) -> tuple[int, int]:
        low = u.lower()
        if re.search(r"metric|0057", low):
            return (0, len(u))
        if re.search(r"prior.?auth|preauth|authoriz", low):
            return (1, len(u))
        if re.search(r"interoper|transparen|discl", low):
            return (2, len(u))
        return (3, len(u))

    return sorted(dict.fromkeys(urls), key=rank)[:cap]


def crawl(domain: str, max_pages: int, pause: float) -> tuple[list[dict], list[str]]:
    """
    Harvest disclosure links from a domain's own hub pages.

    Seeded from the sitemap rather than from the home page. When there is no
    sitemap the domain is skipped and said so: a CDN host serves documents and
    links to nothing, so there is nothing to crawl there -- its documents are
    reached from the brand site that links them.
    """
    sitemaps, disallow = robots(domain)
    seeds = hub_pages(domain, max_pages)
    notes: list[str] = []
    if not seeds:
        return [], ["no sitemap, or nothing in it looked like a hub"]

    found: dict[str, dict] = {}
    for url in seeds:
        if blocked(url, disallow):
            continue
        try:
            blob, ctype, final = fetch(url, timeout=25)
        except Exception:                                    # noqa: BLE001
            continue
        for href, text in links(final, blob):
            href = href.split("#")[0]
            if SKIP.search(href) or not href.startswith("http"):
                continue
            hay = href + " " + text
            if not (SIG_A.search(hay) and SIG_B.search(hay)):
                continue
            if not (href.lower().endswith(DOC_EXT) or SIG_B.search(href)):
                continue
            rule = blocked(href, disallow) if host_of(href) == domain else None
            found.setdefault(href, {
                "domain": domain, "url": href, "anchor": text[:90],
                "from": final, "blocked_by": rule or "",
            })
        time.sleep(pause)

    nblocked = sum(1 for f in found.values() if f["blocked_by"])
    if nblocked:
        notes.append(f"{nblocked} blocked by robots.txt")
    notes.append(f"{len(seeds)} hub pages")
    return list(found.values()), notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", required=True, help="file of domains, one per line")
    ap.add_argument("--max-pages", type=int, default=60, help="pages crawled per domain")
    ap.add_argument("--workers", type=int, default=6, help="domains crawled at once")
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--crawl-only", action="store_true")
    args = ap.parse_args()

    doms = [d.strip() for d in Path(args.domains).read_text().splitlines() if d.strip()]
    if args.limit:
        doms = doms[: args.limit]
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"crawling {len(doms)} known publisher domains, {args.max_pages} pages each",
          flush=True)
    cands: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for dom, (rows, notes) in zip(doms, pool.map(
                lambda d: crawl(d, args.max_pages, args.pause), doms)):
            cands += rows
            print(f"  {dom:<44} {len(rows):>4} candidates"
                  + ("  " + "; ".join(notes) if notes else ""), flush=True)

    with open(OUT / "sweep_candidates.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["domain", "url", "anchor", "from", "blocked_by"])
        w.writeheader()
        w.writerows(cands)
    print(f"\n{len(cands)} candidate documents -> out/preauth/sweep_candidates.csv", flush=True)
    if args.crawl_only:
        return 0

    # Fetch. Anything already in the store is returned from cache, so a repeat
    # run costs nothing for what it already has.
    todo = [c["url"] for c in cands if not c["blocked_by"]]
    print(f"fetching {len(todo)} documents", flush=True)
    ok = err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for m in pool.map(grab, todo):
            if m.get("error"):
                err += 1
            else:
                ok += 1
    print(f"  {ok} stored, {err} failed", flush=True)

    rows = extract.run("cms")
    flagged = [r for r in rows if r.get("needs_review")]
    rows = [r for r in rows if not r.get("needs_review")]
    (OUT / "needs_review.json").write_text(json.dumps(flagged, indent=1) + "\n")
    (DATA / "seg_w5_cms_template.json").write_text(
        json.dumps({"filings": rows}, indent=1) + "\n")
    print(f"\n{len(rows)} filings parsed, {len(flagged)} held for review "
          f"-> data/seg_w5_cms_template.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
