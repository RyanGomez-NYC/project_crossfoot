"""
Validation rules for CMS-0057-F prior authorization filings.

This is the part that does not exist anywhere else. CMS mandates publication
but does not mandate a format, does not require counts, and does not check
arithmetic. These rules catch filings whose own numbers do not reconcile.

Each rule returns a list of findings. A finding is:
    {filing_id, rule, severity, detail}
severity: "error"   -> the filing contradicts itself
          "warn"    -> implausible but not provably wrong
          "info"    -> a coverage gap worth recording
"""
from __future__ import annotations

TOLERANCE_PCT = 1.0          # printed vs computed percentage points
RECONCILE_TOLERANCE = 0.005  # 0.5% of total may be unaccounted (pending, withdrawn)


def _f(fid, rule, severity, detail):
    return {"filing_id": fid, "rule": rule, "severity": severity, "detail": detail}


def counts_reconcile(r):
    """approved + denied should account for the stated total."""
    out = []
    for tag in ("std", "exp"):
        total, appr, den = r.get(f"{tag}_total"), r.get(f"{tag}_approved"), r.get(f"{tag}_denied")
        if None in (total, appr, den) or total == 0:
            continue
        gap = total - (appr + den)
        if abs(gap) <= max(1, total * RECONCILE_TOLERANCE):
            continue

        # A filing may report extended-review outcomes as a separate bucket.
        ext = r.get("ext_review_approved") or 0
        if ext and abs(gap - ext) <= max(1, total * RECONCILE_TOLERANCE):
            out.append(_f(
                r["filing_id"], f"{tag}_headline_excludes_extended", "warn",
                f"headline approved/denied cover {appr + den:,} of {total:,}; "
                f"the {gap:,} balance is reported separately as extended review, so "
                f"the published percentages understate the full population",
            ))
            continue

        out.append(_f(
            r["filing_id"], f"{tag}_counts_reconcile", "error",
            f"{appr:,} approved + {den:,} denied = {appr + den:,}, "
            f"but total is {total:,} ({gap:,} unaccounted, {abs(gap) / total:.1%})",
        ))
    return out


def printed_pct_matches(r):
    """The payer's printed denial percentage should match its own counts."""
    out = []
    computed, printed = r.get("std_denial_rate"), r.get("std_denial_rate_printed")
    if computed is not None and printed is not None:
        if abs(computed - printed) > TOLERANCE_PCT:
            out.append(_f(
                r["filing_id"], "printed_pct_mismatch", "error",
                f"printed denial rate {printed}% vs {computed}% computed from counts",
            ))
    return out


def appeals_sane(r):
    out = []
    tot, over = r.get("std_appeals_total"), r.get("std_appeals_overturned")
    if tot is not None and over is not None and over > tot:
        out.append(_f(r["filing_id"], "appeals_exceed_total", "error",
                      f"{over:,} overturned exceeds {tot:,} appeals filed"))
    if tot is None and over is None:
        out.append(_f(r["filing_id"], "appeals_not_reported", "info",
                      "no appeal outcome data published"))
    return out


def expedited_not_slower(r):
    """Expedited decisions taking longer than standard indicates transposed columns."""
    out = []
    std_h = r.get("std_tat_mean_days")
    exp_h = r.get("exp_tat_mean_hours")
    if std_h is None or exp_h is None:
        return out
    if exp_h > std_h * 24:
        out.append(_f(r["filing_id"], "expedited_slower_than_standard", "warn",
                      f"expedited mean {exp_h:.1f}h exceeds standard mean "
                      f"{std_h:.2f}d ({std_h * 24:.0f}h)"))
    return out


def expedited_volume_plausible(r):
    """Expedited requests outnumbering standard is nearly always a transposition."""
    out = []
    s, e = r.get("std_total"), r.get("exp_total")
    if s and e and e > s:
        out.append(_f(r["filing_id"], "expedited_exceeds_standard_volume", "warn",
                      f"expedited volume {e:,} exceeds standard {s:,} "
                      f"({e / s:.1f}x) - columns may be transposed"))
    return out


def denial_rate_band(r):
    """Rates at the extremes deserve a look before anyone quotes them."""
    out = []
    d = r.get("std_denial_rate")
    if d is None:
        return out
    if d == 0:
        out.append(_f(r["filing_id"], "zero_denial_rate", "warn",
                      "0% denial rate reported"))
    elif d > 40:
        out.append(_f(r["filing_id"], "high_denial_rate", "warn",
                      f"{d}% standard denial rate is far above the peer range"))
    return out


def counts_published(r):
    out = []
    if not r.get("reports_counts"):
        out.append(_f(r["filing_id"], "percentages_only", "info",
                      "filing publishes percentages without underlying counts; "
                      "arithmetic cannot be verified"))
    return out


RULES = [
    counts_reconcile, printed_pct_matches, appeals_sane,
    expedited_not_slower, expedited_volume_plausible,
    denial_rate_band, counts_published,
]


def run(rows):
    findings = []
    for r in rows:
        for rule in RULES:
            findings.extend(rule(r))
    return findings


if __name__ == "__main__":
    from normalize import load
    fs = run(load())
    for f in fs:
        print(f"[{f['severity']:5}] {f['filing_id']:24} {f['rule']:34} {f['detail']}")
    print(f"\n{len(fs)} findings")
