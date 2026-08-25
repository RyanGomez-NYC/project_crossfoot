```
                        P  R  O  J  E  C  T
  _____ _____   ____   _____ _____ ______ ____   ____ _______
 / ____|  __ \ / __ \ / ____/ ____|  ____/ __ \ / __ \__   __|
| |    | |__) | |  | | (___| (___ | |__ | |  | | |  | | | |
| |    |  _  /| |  | |\___ \\___ \|  __|| |  | | |  | | | |
| |____| | \ \| |__| |____) |___) | |   | |__| | |__| | | |
 \_____|_|  \_\\____/|_____/_____/|_|    \____/ \____/  |_|

        Democratizing publicly funded healthcare data
              https://ryangomez.nyc/crossfoot
```

## The name

To **crossfoot** a ledger is to add it across, add it down, and check that the
two totals agree — the oldest, plainest test of whether numbers hold together.

```
                      approved   denied   total
                     ┌─────────┬────────┬────────┐
        standard     │  41,203 │  5,914 │ 47,117 │  ✓
        expedited    │   3,861 │    442 │  4,303 │  ✓
                     ├─────────┼────────┼────────┤
        total        │  45,064 │  6,356 │ 51,420 │
                     └─────────┴────────┴────────┘
                          ✓         ✓        ✓
             across agrees ── down agrees ── it crossfoots
```

That is what this project does to the healthcare system's own published
figures. When the totals do not agree — and often they do not — the
disagreement is published as a finding, not smoothed over.

## The mission

Vast parts of US healthcare run on public money, and the law obliges the plans
and programs that spend it to report how it is used — what gets approved and
denied, how long decisions take, what happens on appeal, what care actually
costs. In practice that reporting lands scattered across hundreds of payer and
hospital websites, as PDFs, spreadsheets, scans, and pages that only assemble
in a browser, with nobody checking the arithmetic.

**Data that is technically public and practically unreadable is not public in
any way that matters.** Project Crossfoot collects it, validates it against
itself, and publishes it where anyone can check and use it — no middleman
between the public and its own data.

## What the data says

Live figures from the site as of 2026-08-25 — the dataset grows in crawl
waves, so these are a floor, not a ceiling:

```
   PRIOR AUTHORIZATION · CY2025 · CMS-0057-F
   658 filings · 124 insurers · 48 states

   requests decided   ██████████████████████████████████  73,167,403
   denied             ████                                 7,752,713
   denials appealed   ██                                   3,560,291
   denials overturned ▏                                      130,906

   A no that nobody checks usually stands.
   130,906 times, somebody checked.
```

```
   HOSPITAL PRICES
   16,201,562 price line-items · 5,381 hospitals · 3,152 counties

   what hospitals bill for every $1.00 actually paid:  $5.03
```

Every one of those numbers is computed live from the database on the site —
none is typed in — and every row traces to the payer's or hospital's own
document.

## How it works

```
        hundreds of payer & hospital websites
   ┌────────┬────────┬────────┬────────┬────────┐
   │  PDFs  │  xlsx  │ scans  │  HTML  │  JSON  │
   └───┬────┴───┬────┴───┬────┴───┬────┴───┬────┘
       │        │        │        │        │
       ▼        ▼        ▼        ▼        ▼
   ┌─────────────────────────────────────────────┐
   │  CRAWL      pipeline/prices/fetch.py        │  retries, TLS-walled hosts,
   │             scripts/recover_*.py            │  provenance hashes
   └──────────────────────┬──────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────┐
   │  HARVEST    tools/harvest.py                │  the unreadable ones:
   │             scripts/harvest_large.py        │  scans → page images,
   │                                             │  xlsx → text grids,
   │                                             │  browser-only → rendered DOM
   └──────────────────────┬──────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────┐
   │  TRANSCRIBE   AI agents read the artifacts  │  counts copied exactly as
   │               and copy out the counts       │  printed — and only counts;
   │                                             │  never a rate, never a guess
   └──────────────────────┬──────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────┐
   │  COMPUTE    filings/normalize.py, merge.py  │  every rate derived from
   │             pipeline/prices/mrf.py          │  counts by this code
   └──────────────────────┬──────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────┐
   │  VALIDATE   filings/validate.py             │  does approved + denied
   │             pipeline/prices/validate.py     │  equal the stated total?
   │                                             │  disagreement → finding
   └──────────┬────────────────────┬─────────────┘
              ▼                    ▼
        ┌───────────┐        ┌───────────┐
        │  the data │        │  findings │      both published, together,
        │           │        │  + misses │      free, and traceable
        └───────────┘        └───────────┘
```

The division of labor is the whole method: **AI agents read and transcribe;
code does the arithmetic.** A model never computes a rate, and code never
guesses at a number a document did not print.

## The rules the work lives by

1. **Every figure traces to a source.** Each number carries the URL and a
   SHA-256 of the bytes the payer actually served. If a payer later edits or
   moves the file, the record of what it said remains.
2. **Computed, never copied.** Every rate is derived in code from extracted
   counts, then compared against the rate the payer printed. Where the two
   disagree, that is a finding.
3. **A null is never a zero.** A payer that published no number is recorded as
   silent, not as zero. "This payer denied nothing" and "this payer published
   nothing" are different claims.
4. **Contradictions are published, not smoothed.** No judgment call picks a
   winner between numbers that disagree; the disagreement ships as a finding
   with the arithmetic shown.
5. **The misses ship with the findings.** Every document that could not be
   read — wrong format, dead link, refused connection — is catalogued and
   published beside the data. A dataset that hides what it missed is asking to
   be trusted rather than checked.
6. **The pipeline is public.** This repository. Rerun the work, or point the
   same machinery at data nobody has read yet.

## What's in this repository

```
project_crossfoot/
│
├── data/
│   ├── filings_top10.json        the ten biggest filings, as extracted
│   └── pa_metrics_top10.csv      the same ten, normalized — computed rates,
│                                 quality flags
├── examples/
│   └── pa_metrics_top10.xlsx     formatted workbook: Metrics + Validation
│
├── filings/                      ── CMS-0057-F prior-authorization pipeline ──
│   ├── normalize.py              raw collector segments → one flat schema
│   ├── merge.py                  dedupe overlapping collector segments
│   ├── validate.py               consistency rules; disagreements → findings
│   ├── gaps.py                   classify coverage gaps (blocked / missing / …)
│   ├── build.py                  build the published CSV + validation report
│   ├── export_xlsx.py            formatted workbook for Excel / Power BI
│   └── render.py                 self-contained HTML page of the dataset
│
├── pipeline/prices/              ── hospital price transparency pipeline ──
│   ├── sources.py                seed list of hospitals and their MRF locations
│   ├── fetch.py                  the crawler: fetch, retry, provenance hashes
│   ├── mrf.py                    parse CMS Hospital Price Transparency files
│   ├── codes.py                  procedure code handling
│   ├── medicare.py               Medicare reference prices
│   ├── counties.py               county-level assembly
│   ├── enrich.py                 join external covariates
│   ├── basket.py                 the fixed basket of shoppable services
│   ├── validate.py               consistency checks on extracted prices
│   ├── xlsx.py                   minimal xlsx reader/writer (stdlib only)
│   ├── build.py                  orchestrates the whole prices pipeline
│   └── selftest.py               self-tests for the above
│
├── analysis/                     ── the models ──
│   ├── county_models.py          county outcomes: medical debt, premature
│   │                             death, years of life lost (trees + ridge,
│   │                             cross-validated, permutation importance)
│   └── hospital_models.py        the fair-price index (two-way fixed effects)
│                                 and estimates for unpublished prices
│                                 (low-rank completion, graded blind)
│
├── scripts/                      ── recovery crawlers ──
│   ├── harvest_large.py          stream-parse price files too big for memory
│   ├── recover_browser.py        TLS-walled hosts, via a browser profile
│   ├── recover_manual.py         hand-seeded special cases
│   └── recover_ca06.py           one specific recovery, kept for the record
│
└── tools/
    ├── harvest.py                unreadable documents → reviewable artifacts
    └── unread.txt                the queue of such documents, by kind
```

## Running it

```bash
python3 -m pip install -r requirements.txt

# the prices pipeline (a package — run from the repository root)
python3 -m pipeline.prices.build

# the filings pipeline (plain scripts — run from inside filings/;
# expects raw collector segments in ../data/, of which this repo
# carries the top-ten sample)
cd filings && python3 build.py

# the models (run after a pipeline build; laptop-sized, never on a web host)
python3 -m analysis.county_models
python3 -m analysis.hospital_models

# the harvester
python3 tools/harvest.py <url> [<url> ...]
python3 tools/harvest.py --list tools/unread.txt
```

Only `analysis/`, `filings/export_xlsx.py`, and the `scripts/` crawlers need
third-party packages; **the two pipelines themselves are standard library**.
The harvester runs on a Mac and needs `openpyxl` for spreadsheets and
`poppler` (`pdftotext`, `pdftoppm`) for PDFs; Chrome is found automatically
for client-rendered pages.

## The models, briefly

**The fair-price index** (`analysis/hospital_models.py`). Every price is two
things at once: the procedure (an MRI costs more than a blood draw everywhere)
and the hospital (some places charge more for everything). A two-way
fixed-effects model over 12.1M published negotiated rates splits them apart,
leaving each of 1,202 hospital price files a single score:

```
                          the going rate
                                │
     cheaper than expected      │      dearer than expected
   ◄────────────────────────────┼────────────────────────────►
          ░░▒▒▒▓▓▓██████████████████████▓▓▓▒▒░░       ●
   ×0.5              ×0.76 ─ half ─ ×1.30        ×2       ×4
                     of all hospitals                  the most
                     land in this band                 expensive
```

×1.20 means a file's rates run 20% above what its own mix of services would
predict; the item and hospital effects together explain 91% of all price
variation. A second stage asks what predicts the score; a completion model
estimates the prices a file did not publish and is graded only on prices it
was never shown.

**The county models** (`analysis/county_models.py`). For medical debt,
premature death, and years of life lost: gradient-boosted trees checked by a
ridge, trained on ~3,100 counties and validated on counties they never saw,
with permutation importance and partial dependence for what actually moves
them. None of it is causal, and the code says so.

## The ten example filings

Ranked by total denials reported, standard plus expedited. Only filings that
published counts can rank here: a filing that printed percentages with no
counts behind them reports a rate nobody can check, not a number of denials.

| Filing | Organization | Plan | Coverage | Denials reported |
|---|---|---|---|---:|
| f0111 | California DHCS | Medi-Cal Dental Fee-for-Service | Medicaid FFS | 926,119 |
| f0469 | UnitedHealth Group | UnitedHealthcare Medicare Advantage — Contract H2001 | Medicare Advantage | 443,380 |
| f0195 | Colorado HCPF | Health First Colorado / CHP+ (13 PA programs combined) | Medicaid FFS | 435,159 |
| f0352 | Ohio Department of Medicaid | Ohio Medicaid Managed Care — all plans combined | Medicaid Managed Care | 228,626 |
| ca-ccs-2025 | California DHCS | California Children's Services (CCS) | Medicaid FFS | 200,549 |
| f0530 | Virginia DMAS | Virginia Medicaid Fee-for-Service (Cardinal Care FFS) | Medicaid FFS | 180,068 |
| f0494 | UnitedHealth Group | UnitedHealthcare Medicare Advantage — Contract H5253 | Medicare Advantage | 124,196 |
| f0118 | CareSource | CareSource Ohio Medicaid | Medicaid Managed Care | 123,163 |
| f0124 | Centene | Ambetter Health Florida / Celtic (49004) | Marketplace QHP | 94,277 |
| f0151 | Centene | Health Net Medi-Cal | Medicaid Managed Care | 92,863 |

2,848,400 denials across the ten. All ten reconcile against themselves —
approved plus denied matches each stated total, and no validation rule fires
on any of them — so the workbook's Validation sheet carries its header and
nothing else. That is the sheet reporting a fact, not the sheet being
unfinished.

## Reading the data

- **A blank cell is not a zero.** A blank means the payer did not publish that
  number. "This payer denied nothing" and "this payer published nothing" are
  different claims.
- **Every rate is computed.** `std_denial_rate` and `exp_denial_rate` are
  derived from the extracted counts in the same row, never transcribed from
  the document.
- **`quality_flags`** is `clean`, or a semicolon-separated list of
  `severity:rule` for every consistency rule the filing trips.

## The harvester

`tools/harvest.py` handles the documents a plain crawler cannot read — binary
spreadsheets, scanned PDFs with no text layer, pages that only assemble in a
browser. It fetches and mechanically converts, and stops there: it writes
artifacts (a text grid, a page image, a rendered DOM) plus a `SOURCE.txt`
recording the URL, fetch time, content type, and the SHA-256 of the bytes as
delivered, so the numbers eventually published can be traced to the document
the payer actually served. It never produces a filing record — the model reads
and transcribes, code does the arithmetic.

## What is NOT here

This repository is the pipeline, the models, and a working sample — code that
anyone can run and check. The website that presents the full datasets, and the
database behind it, are not part of this release. The full data itself is free
on the site — every table as CSV and JSON through a read-only API, no key, no
account:

```
   the data, live and free ──►  https://ryangomez.nyc/crossfoot
```

## Contributing

The work is sized for many hands, and no healthcare background is needed —
the whole method is that the documents speak for themselves.

- **Report** a filing that is missing or misread — open an issue with the
  link; traceability is the whole product.
- **Harvest** a document that resists machines — `tools/unread.txt` lists the
  queue; each one recovered is data nobody else has.
- **Propose** the next dataset — publicly funded healthcare publishes more
  mandated reporting than anyone reads.

```
      add it across  ─┬─  add it down
                      │
              do they agree?
                      │
             ┌────────┴────────┐
            yes                no
             │                 │
        publish it      publish that too
```
