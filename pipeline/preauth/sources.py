"""
Where the reporting universe comes from.

CMS-0057-F obliges four kinds of payer to publish CY2025 prior authorization
metrics, each at its own level:

    Medicare Advantage organizations      per contract
    Medicaid managed care plans           per plan
    CHIP managed care entities            per plan
    QHP issuers on the FFEs               per issuer
    State Medicaid / CHIP FFS programs    per state

Nothing in this file is a filing. These are the documents that tell us *who is
obliged to file* — the denominator. Without it, "448 filings" is a count with no
population behind it and no way to say who is out of compliance.

Every URL is pinned and was verified on 24 August 2026. The CMS zips follow one
pattern -- https://www.cms.gov/files/zip/<page-slug>.zip -- and are refreshed in
place each month, so the pin does not rot; the file inside carries its own
vintage in the name (MA_Cnty_SA_2026_07, CPSC_Enrollment_2026_07).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    url: str
    local: str            # directory under data/raw/preauth/
    gives: str
    landing: str


SOURCES: dict[str, Source] = {
    "ma_directory": Source(
        key="ma_directory",
        title="MA Plan Directory (contract directory)",
        url="https://www.cms.gov/files/zip/ma-plan-directory.zip",
        local="ma_plan_directory",
        gives="contract ID, legal entity, marketing name, organization type, "
              "parent organization, total enrollment, contract effective date",
        landing="https://www.cms.gov/data-research/statistics-trends-and-reports/"
                "medicare-advantagepart-d-contract-and-enrollment-data/ma-plan-directory",
    ),
    "ma_service_area": Source(
        key="ma_service_area",
        title="MA Contract Service Area by State/County",
        url="https://www.cms.gov/files/zip/ma-contract-service-area.zip",
        local="ma_contract_service_area",
        gives="contract ID -> every county and state it is approved to serve. "
              "The contract-to-state map that lets one national filing be "
              "attributed to the states it actually covers.",
        landing="https://www.cms.gov/data-research/statistics-trends-and-reports/"
                "medicare-advantagepart-d-contract-and-enrollment-data/"
                "ma-contract-service-area-state/county",
    ),
    "cpsc_enrollment": Source(
        key="cpsc_enrollment",
        title="Monthly Enrollment by Contract/Plan/State/County",
        url="https://www.cms.gov/files/zip/monthly-enrollment-contract-plan-state-county.zip",
        local="monthly_enrollment_contract_plan_state_county",
        gives="enrollment by contract x plan x state x county, and a contract "
              "info table with parent organization. Enrollment counts under 11 "
              "are suppressed as '*' -- they are censored, not zero.",
        landing="https://www.cms.gov/data-research/statistics-trends-and-reports/"
                "medicare-advantagepart-d-contract-and-enrollment-data/"
                "monthly-enrollment-contract/plan/state/county",
    ),
}

# Organization types that are Medicare Advantage organizations under the rule.
# PACE, 1876 Cost, HCPP and the LI NET sponsor are none of them: they are not MA
# organizations and owe no CMS-0057-F disclosure. Dropping them takes the
# contract directory from 921 rows to the ~700 that are actually obliged.
MA_ORG_TYPES = {"Local CCP", "Regional CCP", "PFFS", "MSA"}
