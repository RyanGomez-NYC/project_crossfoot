```
  _____ _____   ____   _____ _____ ______ ____   ____ _______
 / ____|  __ \ / __ \ / ____/ ____|  ____/ __ \ / __ \__   __|
| |    | |__) | |  | | (___| (___ | |__ | |  | | |  | | | |
| |    |  _  /| |  | |\___ \\___ \|  __|| |  | | |  | | | |
| |____| | \ \| |__| |____) |___) | |   | |__| | |__| | | |
 \_____|_|  \_\\____/|_____/_____/|_|    \____/ \____/  |_|
```

**Democratizing publicly funded healthcare data**

Crossfoot's mission is to democratize publicly funded healthcare data. US law
requires health plans and public programs to disclose a great deal about how the
system actually runs, and most of it lands scattered across payer websites in
formats nobody reads. Crossfoot draws on that legislatively mandated reporting
and other publicly available data and turns it into datasets anyone can check
and use — no middleman between the public and its own data — putting light into
the industry's dark corners so ideas for better care at the best cost can stand
on numbers.

It starts with prior authorization denials: a crawled, normalized, and
**validated** dataset of the plan-level disclosures US health plans are required
to publish under CMS-0057-F (CY2025). Every row is traced to the payer's or
state agency's own document, and every rate is computed in code from extracted
counts — never taken from a percentage the payer printed.

This repository holds a working example of it: the document harvester, this
documentation, and the ten filings that reported more denials than any others in
the full 536-filing dataset.

## What's here

```
data/filings_top10.json          the ten filings as extracted — one raw record per filing
data/pa_metrics_top10.csv        the same ten, normalized — computed rates, quality flags
examples/pa_metrics_top10.xlsx   formatted workbook, two sheets: Metrics and Validation

tools/harvest.py                 turns documents a crawler cannot read into artifacts a reader can
tools/unread.txt                 the queue of such documents, by kind
```

## The ten filings

Ranked by total denials reported, standard plus expedited. Only filings that
published counts can rank here: a filing that printed percentages with no counts
behind them reports a rate nobody can check, not a number of denials.

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
approved plus denied matches each stated total, and no validation rule fires on
any of them — so the workbook's Validation sheet carries its header and nothing
else. That is the sheet reporting a fact, not the sheet being unfinished.

## Reading the data

- **A blank cell is not a zero.** A blank means the payer did not publish that
  number. "This payer denied nothing" and "this payer published nothing" are
  different claims.
- **Every rate is computed.** `std_denial_rate` and `exp_denial_rate` are derived
  from the extracted counts in the same row, never transcribed from the document.
- **`quality_flags`** is `clean`, or a semicolon-separated list of
  `severity:rule` for every consistency rule the filing trips.

## The harvester

`tools/harvest.py` handles the documents a plain crawler cannot read — binary
spreadsheets, scanned PDFs with no text layer, pages that only assemble in a
browser. It fetches and mechanically converts, and stops there: it writes
artifacts (a text grid, a page image, a rendered DOM) plus a `SOURCE.txt`
recording the URL, fetch time, content type, and the SHA-256 of the bytes as
delivered, so the numbers eventually published can be traced to the document the
payer actually served. It never produces a filing record — the model reads and
transcribes, code does the arithmetic.

```bash
python3 tools/harvest.py <url> [<url> ...]
python3 tools/harvest.py --list tools/unread.txt
```

Runs on a Mac. Needs `openpyxl` for spreadsheets and `poppler` (`pdftotext`,
`pdftoppm`) for PDFs; Chrome is found automatically for client-rendered pages.
