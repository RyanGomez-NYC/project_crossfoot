"""
Find where a payer or a state agency published its disclosure.

Discovery is the bottleneck, not extraction. The extractor already reads the CMS
template wherever it appears; what is missing for most of the uncovered universe
is the URL. This module goes looking, cheapest method first, and never guesses a
filing -- it produces candidate URLs with a reason, and docs.py decides whether
they are real by fetching them.

Order of attack, per domain:

  1. robots.txt -> declared sitemaps -> every URL in them, filtered on the
     vocabulary of the rule. A sitemap is the site telling you what it has; it
     costs one request and beats any amount of guessing.
  2. a path list learned from the documents already collected, tried directly.
     Cheap, and the slugs repeat across payers because they all read the same
     CMS guidance.
  3. the links on any page found by 1 or 2, one hop, kept if they look like a
     document.

robots.txt is obeyed. A payer that disallows the path its legally mandated
disclosure sits on has told us something worth recording, and the recording is
the point -- routing around it would destroy the finding.

    python3 -m pipeline.preauth.discover --domains data/seeds/pa_domains.csv
    python3 -m pipeline.preauth.discover example.org [example.net ...]

Writes out/preauth/candidates.csv: domain, url, how it was found, score.
"""
from __future__ import annotations

import argparse
import csv
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .docs import UA, fetch, links

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "preauth"

# What a disclosure's URL or link text looks like. Deliberately loose: this is a
# filter over a list the site handed us, not a guess about what exists.
WANT = re.compile(r"prior.?auth|preauth|pa.?metric|0057|interoperab", re.I)
METRIC = re.compile(r"metric|report|disclosur|transparen|statistic|data", re.I)

# Slugs seen on documents already collected, plus the obvious variants. Tried in
# order against each domain and each of a few common prefixes.
PATHS = [
    "prior-authorization-metrics",
    "prior-authorization-metrics-reporting",
    "prior-auth-metrics",
    "prior-authorization-metrics-report",
    "pa-metrics",
    "cms-interoperability-and-prior-authorization-final-rule",
    "cms-interoperability",
    "interoperability",
]
PREFIXES = ["", "providers/", "provider/", "legal/"]

MAX_SITEMAP_BYTES = 12_000_000
MAX_SITEMAPS = 12


def host_of(url: str) -> str:
    """Registrable host, "www." stripped -- see the note in sweep.py."""
    return re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc.lower())


def robots(domain: str) -> tuple[list[str], list[str]]:
    """(sitemap urls, disallowed path prefixes) for the wildcard agent."""
    sitemaps, disallow, applies = [], [], False
    try:
        blob, _, _ = fetch(f"https://{domain}/robots.txt", timeout=25)
    except Exception:                                        # noqa: BLE001
        return [], []
    for line in blob.decode("utf-8", "replace").splitlines():
        line = line.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            applies = val == "*"
        elif key == "sitemap":
            sitemaps.append(val)
        elif key == "disallow" and applies and val:
            disallow.append(val)
    return sitemaps, disallow


def blocked(url: str, disallow: list[str]) -> str | None:
    """The robots rule that forbids this URL, if any."""
    path = urllib.parse.urlparse(url).path or "/"
    for rule in disallow:
        if rule.endswith("$"):
            if re.search(rule.replace("*", ".*"), path):
                return rule
        elif "*" in rule:
            if re.match(rule.replace("*", ".*"), path):
                return rule
        elif path.startswith(rule):
            return rule
    return None


def sitemap_urls(url: str, seen: set[str], depth: int = 0) -> list[str]:
    """Every <loc> in a sitemap, following sitemap indexes one level."""
    if url in seen or depth > 1 or len(seen) > MAX_SITEMAPS:
        return []
    seen.add(url)
    try:
        blob, _, _ = fetch(url, timeout=60)
    except Exception:                                        # noqa: BLE001
        return []
    if len(blob) > MAX_SITEMAP_BYTES:
        blob = blob[:MAX_SITEMAP_BYTES]
    text = blob.decode("utf-8", "replace")
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
    out = []
    for loc in locs:
        if loc.endswith((".xml", ".xml.gz")) and "sitemap" in loc.lower():
            out += sitemap_urls(loc, seen, depth + 1)
        else:
            out.append(loc)
    return out


def probe(domain: str, disallow: list[str], pause: float) -> list[tuple[str, str]]:
    """
    Try the learned slugs directly. Returns (url, how) for each 200.

    Probes run concurrently and with a short timeout: most of them are misses,
    and a miss on a slow government host otherwise costs the whole budget. The
    worker count is small enough to stay polite to one host.
    """
    urls = [f"https://{domain}/{prefix}{slug}"
            for prefix in PREFIXES for slug in PATHS
            if not blocked(f"https://{domain}/{prefix}{slug}", disallow)]

    def one(url: str) -> str | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.geturl() if r.status == 200 else None
        except Exception:                                    # noqa: BLE001
            return None

    hits = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for got in pool.map(one, urls):
            if got:
                hits.append((got, "path"))
    # A site that answers 200 to every guess is not agreeing with all of them --
    # it is serving a soft 404, or a bot wall, under whatever path is asked for.
    # Taking those as hits floods the candidate list with fiction, so a run that
    # succeeds on more than a quarter of its guesses is discarded entirely.
    if len(hits) > max(3, len(urls) // 4):
        return []
    return hits


def discover(domain: str, pause: float = 0.3) -> list[dict]:
    domain = re.sub(r"^https?://", "", domain).strip("/")
    sitemaps, disallow = robots(domain)
    found: dict[str, dict] = {}

    seen: set[str] = set()
    for sm in sitemaps[:MAX_SITEMAPS]:
        for u in sitemap_urls(sm, seen):
            if WANT.search(u) and METRIC.search(u):
                found[u] = {"domain": domain, "url": u, "how": "sitemap", "score": 2}
            elif WANT.search(u):
                found.setdefault(u, {"domain": domain, "url": u, "how": "sitemap", "score": 1})

    if not found:
        for u, how in probe(domain, disallow, pause):
            found[u] = {"domain": domain, "url": u, "how": how, "score": 2}

    # one hop out of the best pages, for the document the page links to
    for u in sorted(found, key=lambda k: -found[k]["score"])[:4]:
        try:
            blob, _, final = fetch(u, timeout=45)
        except Exception:                                    # noqa: BLE001
            continue
        for href, text in links(final, blob):
            if not href.lower().endswith((".pdf", ".xlsx", ".ashx", ".aspx")):
                continue
            if not (WANT.search(href + " " + text) and METRIC.search(href + " " + text)):
                continue
            rule = blocked(href, disallow) if host_of(href) == host_of("https://" + domain) else None
            found[href] = {"domain": domain, "url": href,
                           "how": f"link from {u}" if not rule else f"robots-blocked ({rule})",
                           "score": 0 if rule else 3}
        time.sleep(pause)

    for rec in found.values():
        rec["robots_disallow"] = "|".join(disallow[:6])
    return sorted(found.values(), key=lambda r: -r["score"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("domains", nargs="*")
    ap.add_argument("--domains-file", help="CSV with a 'domain' column")
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--out", default=str(OUT / "candidates.csv"))
    args = ap.parse_args()

    doms = list(args.domains)
    labels: dict[str, str] = {}
    if args.domains_file:
        with open(args.domains_file, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                doms.append(row["domain"])
                labels[row["domain"]] = row.get("label", "")
    if not doms:
        ap.error("give a domain or --domains-file")

    OUT.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for d in doms:
        try:
            rows = discover(d, args.pause)
        except Exception as e:                               # noqa: BLE001
            print(f"{d:<32} error {type(e).__name__}: {e}", flush=True)
            continue
        for r in rows:
            r["label"] = labels.get(d, "")
        all_rows += rows
        best = rows[0]["url"][:78] if rows else ""
        print(f"{d:<32} {len(rows):>3} candidates  {best}", flush=True)

    fields = ["label", "domain", "url", "how", "score", "robots_disallow"]
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    print(f"\n{len(all_rows)} candidates -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
