"""
Crossfoot — healthcare pricing collection.

Second analysis. Gathers every public price a hospital or the Medicare program
publishes, normalizes it, checks each source against itself, and joins it to
county-level medical debt and health outcomes so cost, debt and access can be
compared across regions and demographics.

Everything here is standard library only and runs on a Mac:

    python3 -m pipeline.prices.build --all

Sub-modules:
    fetch       HTTP with provenance — every byte fetched gets a SOURCE record
    sources     the registry of what is collected and where it comes from
    xlsx        a minimal .xlsx reader (zipfile + xml), for Urban's workbooks
    basket      the fixed list of procedures compared across hospitals
    medicare    Medicare inpatient / outpatient / physician utilization & payment
    counties    County Health Rankings + Debt in America + ZIP→county crosswalk
    mrf         hospital price-transparency machine-readable files (sampled)
    validate    the consistency rules — the part that does not exist elsewhere
    build       the orchestrator; writes out/prices/ and the abridged data/
"""
