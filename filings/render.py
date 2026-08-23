"""Render the dataset to a self-contained HTML page."""
from __future__ import annotations
import csv, html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"

# dataviz reference palette, slots 1-6, validated light + dark
CAT = {
    "Medicare Advantage":     ("#2a78d6", "#3987e5"),
    "Medicaid Managed Care":  ("#eb6834", "#d95926"),
    "Marketplace QHP":        ("#1baf7a", "#199e70"),
    "Medicaid FFS":           ("#eda100", "#c98500"),
    "CHIP Managed Care":      ("#e87ba4", "#d55181"),
    "Medicare-Medicaid Plan": ("#008300", "#008300"),
}
MIN_VOL = 1000   # below this, a denial rate is noise
TOP_N = 30


def num(v):
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def bar_rows(items, key, label, sub, colorer, fmt="{:.1f}%"):
    top = max(key(i) for i in items)
    out = []
    for i in items:
        v, idx = key(i), colorer(i)
        out.append(f'''<div class="row" title="{html.escape(sub(i))}">
  <div class="lbl">{label(i)}</div>
  <div class="track"><div class="bar" style="width:{v / top * 100:.1f}%;background:var(--c{idx})"></div>
    <span class="val">{fmt.format(v)}</span></div>
</div>''')
    return "".join(out)


def build_html():
    rows = list(csv.DictReader((OUT / "pa_metrics_2025.csv").open()))
    findings = list(csv.DictReader((OUT / "validation_findings.csv").open()))

    orgs = len({r["parent_org"] for r in rows})
    states = len({r["state"] for r in rows if r["state"]})
    tot = sum(num(r["std_total"]) or 0 for r in rows)
    den = sum(num(r["std_denied"]) or 0 for r in rows)
    weighted = 100 * den / tot if tot else 0
    contradicting = {f["filing_id"] for f in findings if f["severity"] == "error"}
    flagged = {f["filing_id"] for f in findings if f["severity"] in ("error", "warn")}
    no_counts = sum(1 for r in rows if r["reports_counts"] != "True")
    no_appeals = sum(1 for r in rows if not r["std_appeal_overturn_rate"])

    # aggregate by coverage type, weighted by volume
    agg = {}
    for r in rows:
        t, d = num(r["std_total"]), num(r["std_denied"])
        if t and d is not None:
            a = agg.setdefault(r["coverage_type"], [0, 0, 0])
            a[0] += t; a[1] += d; a[2] += 1
    agg_items = sorted(
        [(k, 100 * v[1] / v[0], v[0], v[2]) for k, v in agg.items() if k in CAT],
        key=lambda x: -x[1])

    cov_bars = bar_rows(
        agg_items,
        key=lambda i: i[1],
        label=lambda i: html.escape(i[0]),
        sub=lambda i: f"{i[0]}: {i[1]:.2f}% denied across {i[2]:,.0f} standard requests in {i[3]} filings",
        colorer=lambda i: list(CAT).index(i[0]),
        fmt="{:.1f}%")

    # top plans by denial rate, volume-gated
    ranked = sorted(
        [r for r in rows
         if num(r["std_denial_rate"]) is not None and (num(r["std_total"]) or 0) >= MIN_VOL],
        key=lambda r: -num(r["std_denial_rate"]))[:TOP_N]

    def plan_label(r):
        bad = r["filing_id"] in contradicting
        nm = html.escape(r["plan_name"][:46])
        return nm + (' <b class="fl">!</b>' if bad else "")

    plan_bars = bar_rows(
        ranked,
        key=lambda r: num(r["std_denial_rate"]),
        label=plan_label,
        sub=lambda r: (f"{r['plan_name']} - {r['parent_org']}\n{r['coverage_type']}"
                       f"{' - ' + r['state'] if r['state'] else ''}\n"
                       f"{num(r['std_denied']):,.0f} denied of {num(r['std_total']):,.0f}\n"
                       f"Quality: {r['quality_flags']}"),
        colorer=lambda r: list(CAT).index(r["coverage_type"]) if r["coverage_type"] in CAT else 0)

    legend = "".join(
        f'<span class="lg"><i style="background:var(--c{i})"></i>{html.escape(k)}</span>'
        for i, k in enumerate(CAT))

    sev_order = {"error": 0, "warn": 1, "info": 2}
    fnd = "".join(
        f'<tr><td class="{f["severity"]}">{f["severity"]}</td>'
        f'<td>{html.escape(f["rule"])}</td><td>{html.escape(f["detail"])}</td></tr>'
        for f in sorted(findings, key=lambda x: sev_order[x["severity"]])
        if f["severity"] != "info")

    trs = []
    for r in sorted(rows, key=lambda r: (r["parent_org"], r["plan_name"])):
        flags = r["quality_flags"]
        cls = "err" if "error" in flags else ("warn" if "warn" in flags else "ok")
        trs.append(f"""<tr>
<td>{html.escape(r['plan_name'])}</td><td>{html.escape(r['parent_org'])}</td>
<td>{html.escape(r['coverage_type'])}</td><td>{html.escape(r['state'] or '')}</td>
<td class="n">{r['std_total'] or '—'}</td><td class="n">{r['std_denied'] or '—'}</td>
<td class="n">{r['std_denial_rate'] or '—'}</td>
<td class="n">{r['std_appeal_overturn_rate'] or '—'}</td>
<td class="n">{r['std_tat_mean_days'] or '—'}</td>
<td class="{cls}">{html.escape(flags)}</td>
<td><a href="{html.escape(r['source_url'])}">src</a></td></tr>""")

    lightvars = "".join(f"--c{i}:{v[0]};" for i, v in enumerate(CAT.values()))
    darkvars = "".join(f"--c{i}:{v[1]};" for i, v in enumerate(CAT.values()))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prior Authorization Metrics — CY2025 (CMS-0057-F)</title>
<style>
:root{{color-scheme:light;--s1:#fcfcfb;--s2:#f3f3f1;--tp:#0b0b0b;--ts:#52514e;--tm:#8a8880;
--line:#e2e1dd;--err:#c0392b;--warn:#b7791f;{lightvars}}}
@media(prefers-color-scheme:dark){{:root:where(:not([data-theme=light])){{color-scheme:dark;
--s1:#1a1a19;--s2:#232322;--tp:#fff;--ts:#c3c2b7;--tm:#8f8e86;--line:#33332f;
--err:#e66767;--warn:#eda100;{darkvars}}}}}
:root[data-theme=dark]{{color-scheme:dark;--s1:#1a1a19;--s2:#232322;--tp:#fff;--ts:#c3c2b7;
--tm:#8f8e86;--line:#33332f;--err:#e66767;--warn:#eda100;{darkvars}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--s1);color:var(--tp);
font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1200px;margin:0 auto;padding:40px 24px 80px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}}
.sub{{color:var(--ts);margin:0 0 4px}}
.note{{color:var(--tm);font-size:13px;margin:0 0 30px;max-width:760px}}
h2{{font-size:17px;margin:46px 0 6px}}
.h2s{{color:var(--tm);font-size:13px;margin:0 0 18px;max-width:800px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.tile{{background:var(--s2);border:1px solid var(--line);border-radius:10px;padding:15px 17px}}
.tile .k{{font-size:11px;color:var(--tm);text-transform:uppercase;letter-spacing:.05em}}
.tile .v{{font-size:26px;font-weight:600;margin-top:6px;letter-spacing:-.02em}}
.tile .d{{font-size:12px;color:var(--ts);margin-top:3px}}
.legend{{display:flex;flex-wrap:wrap;gap:15px;margin:0 0 18px;font-size:13px;color:var(--ts)}}
.lg{{display:flex;align-items:center;gap:6px}}
.lg i{{width:11px;height:11px;border-radius:3px;display:inline-block}}
.row{{display:grid;grid-template-columns:320px 1fr;gap:12px;align-items:center;margin-bottom:7px}}
.lbl{{font-size:13px;color:var(--ts);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.fl{{color:var(--err)}}
.track{{position:relative;display:flex;align-items:center;height:19px}}
.bar{{height:19px;border-radius:0 4px 4px 0;min-width:2px}}
.val{{font-size:12px;color:var(--ts);margin-left:8px;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}}
th{{text-align:left;color:var(--tm);font-weight:500;border-bottom:1px solid var(--line);
padding:8px 9px;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
td{{padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
td.err,td.error{{color:var(--err)}} td.warn{{color:var(--warn)}} td.ok{{color:var(--tm)}}
a{{color:var(--tp)}}
.scroll{{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px}}
.scroll th{{position:sticky;top:0;background:var(--s1)}}
</style></head><body><div class="wrap">

<h1>Prior authorization metrics, calendar year 2025</h1>
<p class="sub">Plan-level disclosures published under CMS-0057-F, extracted from primary sources and validated.</p>
<p class="note">{len(rows)} filings from {orgs} organizations across {states} states, each traced to the payer's or
state agency's own document. Collected by crawling payer sites directly; no aggregator was used as a source.
Coverage is broad but not complete — {len(findings)} data-quality findings and the known gaps are listed below.</p>

<div class="tiles">
<div class="tile"><div class="k">Filings</div><div class="v">{len(rows)}</div>
  <div class="d">{orgs} organizations, {states} states</div></div>
<div class="tile"><div class="k">Standard requests</div><div class="v">{tot / 1e6:.1f}M</div>
  <div class="d">where counts are published</div></div>
<div class="tile"><div class="k">Weighted denial rate</div><div class="v">{weighted:.1f}%</div>
  <div class="d">denied / total, standard</div></div>
<div class="tile"><div class="k">Self-contradicting</div><div class="v" style="color:var(--err)">{len(contradicting)}</div>
  <div class="d">filings whose own numbers do not reconcile</div></div>
<div class="tile"><div class="k">No counts</div><div class="v">{no_counts}</div>
  <div class="d">publish percentages only</div></div>
<div class="tile"><div class="k">No appeal data</div><div class="v">{no_appeals}</div>
  <div class="d">outcome not published</div></div>
</div>

<h2>Denial rate by coverage type</h2>
<p class="h2s">Volume-weighted across every filing that publishes counts. This is what the aggregate picture
looks like when it is built from the underlying numbers rather than from an average of percentages.</p>
{cov_bars}

<h2>Highest denial rates, plans with 1,000+ standard requests</h2>
<p class="h2s">Volume-gated so that a plan with nine requests and three denials does not top the list.
<b class="fl">!</b> marks a filing whose own published numbers do not reconcile. A further set carry outlier or implausibility warnings; both are itemised below.</p>
<div class="legend">{legend}</div>
{plan_bars}

<h2>Validation findings</h2>
<p class="h2s">Errors and warnings only; coverage-gap notices are omitted here but are carried per row in the
dataset. These rules check each filing against itself. CMS mandates publication but does not mandate a
format, does not require counts, and does not check arithmetic.</p>
<div class="scroll"><table><thead><tr><th>Severity</th><th>Rule</th><th>Detail</th></tr></thead>
<tbody>{fnd}</tbody></table></div>

<h2>Full dataset</h2>
<p class="h2s">Every row links to the source document. Denial and overturn rates are percentages;
turnaround is mean days for standard requests.</p>
<div class="scroll"><table><thead><tr><th>Plan</th><th>Organization</th><th>Coverage</th><th>St</th>
<th>Requests</th><th>Denied</th><th>Denial %</th><th>Overturn %</th><th>TAT d</th>
<th>Quality</th><th>Src</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div>

</div></body></html>"""


if __name__ == "__main__":
    p = OUT / "pa_metrics_2025.html"
    p.write_text(build_html())
    print(f"wrote {p}")
