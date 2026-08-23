"""
Where each dataset comes from.

Every source is pinned to a specific file URL that was verified on 21 August
2026, and most can also be re-discovered from the publisher's catalog so a new
data year is picked up without editing this file. Discovery is optional and
never silent: when it finds a newer file than the pin it says so, and when it
fails it falls back to the pin and says that too.

Nothing here is a price. These are the documents prices are read from.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from . import fetch


@dataclass
class Source:
    key: str
    title: str
    publisher: str
    url: str                       # the pinned file
    local: str                     # file name under data/raw/prices/
    data_year: str                 # what the data describes, not when it was fetched
    license: str
    landing: str
    ckan_id: Optional[str] = None  # catalog.data.gov package id, for discovery
    ckan_match: Optional[str] = None  # regex a resource URL must match to count
    notes: str = ""
    candidates: list = field(default_factory=list)   # URLs to try in order when the pin moves
    optional: bool = False                            # a failed fetch is a note, not an error
    discovered_url: Optional[str] = field(default=None, init=False)


SOURCES: dict[str, Source] = {
    "inpatient": Source(
        key="inpatient",
        title="Medicare Inpatient Hospitals - by Provider and Service",
        publisher="CMS",
        url="https://data.cms.gov/sites/default/files/2026-04/828defb5-c9e6-4442-8c1b-f27bc0799daf/MUP_INP_RY26_P03_V10_DY24_PrvSvc.CSV",
        local="medicare_inpatient_dy24.csv",
        data_year="2024",
        license="public domain (US federal)",
        landing="https://data.cms.gov/provider-summary-by-type-of-service/medicare-inpatient-hospitals/medicare-inpatient-hospitals-by-provider-and-service",
        ckan_id="medicare-inpatient-hospitals-by-provider-and-service",
        ckan_match=r"MUP_INP_RY\d+_P03_V\d+_DY(\d+)_PrvSvc\.CSV$",
        notes="One row per hospital per MS-DRG: discharges, average covered charge, "
              "average total payment, average Medicare payment. Rows with fewer than "
              "11 discharges are suppressed by CMS before publication.",
    ),
    "outpatient": Source(
        key="outpatient",
        title="Medicare Outpatient Hospitals - by Provider and Service",
        publisher="CMS",
        url="https://data.cms.gov/sites/default/files/2026-06/794975f5-e335-4cbf-8e87-982c834a9639/MUP_OUT_RY26_P04_V10_DY24_Prov_Svc.csv",
        local="medicare_outpatient_dy24.csv",
        data_year="2024",
        license="public domain (US federal)",
        landing="https://data.cms.gov/provider-summary-by-type-of-service/medicare-outpatient-hospitals/medicare-outpatient-hospitals-by-provider-and-service",
        ckan_id="medicare-outpatient-hospitals-by-provider-and-service",
        ckan_match=r"MUP_OUT_RY\d+_P04_V\d+_DY(\d+)_Prov_Svc\.csv$",
        notes="One row per hospital per comprehensive APC: beneficiaries, services, "
              "average submitted charge, average Medicare allowed amount, average payment.",
    ),
    "physician_geo": Source(
        key="physician_geo",
        title="Medicare Physician & Other Practitioners - by Geography and Service",
        publisher="CMS",
        url="https://data.cms.gov/sites/default/files/2026-05/e534c74b-79b8-4892-8a95-5a17e2dfec9f/MUP_PHY_R26_P05_V10_D24_Geo.csv",
        local="medicare_physician_geo_dy24.csv",
        data_year="2024",
        license="public domain (US federal)",
        landing="https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/medicare-physician-other-practitioners-by-geography-and-service",
        ckan_id="medicare-physician-other-practitioners-by-geography-and-service",
        ckan_match=r"MUP_PHY_R\d+_P05_V\d+_D(\d+)_Geo\.csv$",
        notes="One row per state (and national) per HCPCS code per place of service: "
              "average submitted charge, Medicare allowed, paid and standardized amounts. "
              "The source of the only public, national price for an office or ED visit.",
    ),
    "chr": Source(
        key="chr",
        title="County Health Rankings 2025 — national analytic data",
        publisher="University of Wisconsin Population Health Institute",
        url="https://www.countyhealthrankings.org/sites/default/files/media/document/analytic_data2025_v3.csv",
        local="chr_analytic_2025.csv",
        data_year="2025 release (measures 2019–2023)",
        license="free for non-commercial use with attribution; see countyhealthrankings.org",
        landing="https://www.countyhealthrankings.org/health-data/methodology-and-sources/data-documentation",
        notes="Two header rows: variable codes, then labels. Outcomes, access and "
              "demographics per county.",
    ),
    "debt_county": Source(
        key="debt_county",
        title="Debt in America 2025 — county-level medical debt",
        publisher="Urban Institute",
        url="https://urban-data-catalog.s3.amazonaws.com/drupal-root-live/2025/11/19/Debt%20in%20America%20County-Level%20Medical%20Debt.xlsx",
        local="urban_debt_county_medical_2025.xlsx",
        data_year="August 2025 credit-bureau panel",
        license="ODC-BY",
        landing="https://datacatalog.urban.org/dataset/debt-america-2025",
        notes="Share of adults with a credit record who have medical debt in collections, "
              "median amount, split by majority-white communities and communities of color. "
              "Suppressed below 50 people. This is debt in collections, not bankruptcy — "
              "no public dataset records the cause of a bankruptcy filing.",
    ),
    "debt_state": Source(
        key="debt_state",
        title="Debt in America 2025 — state-level medical debt",
        publisher="Urban Institute",
        url="https://urban-data-catalog.s3.amazonaws.com/drupal-root-live/2025/11/19/Debt%20in%20America%20State-Level%20Medical%20Debt.xlsx",
        local="urban_debt_state_medical_2025.xlsx",
        data_year="August 2025 credit-bureau panel",
        license="ODC-BY",
        landing="https://datacatalog.urban.org/dataset/debt-america-2025",
    ),
    "debt_national": Source(
        key="debt_national",
        title="Debt in America 2025 — national medical debt",
        publisher="Urban Institute",
        url="https://urban-data-catalog.s3.amazonaws.com/drupal-root-live/2025/11/19/Debt%20in%20America%20National-Level%20Medical%20Debt.xlsx",
        local="urban_debt_national_medical_2025.xlsx",
        data_year="August 2025 credit-bureau panel",
        license="ODC-BY",
        landing="https://datacatalog.urban.org/dataset/debt-america-2025",
        notes="The publisher's own national figure, so the site's headline is theirs and not a reweighting of the county rows.",
    ),
    "places": Source(
        key="places",
        title="CDC PLACES — county health measures, 2025 release",
        publisher="CDC",
        url="https://data.cdc.gov/api/views/i46a-9kgh/rows.csv?accessType=DOWNLOAD",
        local="cdc_places_county_2025.csv",
        data_year="2025 release (BRFSS 2022–2023 model-based estimates)",
        license="public domain (US federal)",
        landing="https://data.cdc.gov/500-Cities-Places/PLACES-County-Data-GIS-Friendly-Format-2025-releas/i46a-9kgh",
        notes="Model-based prevalence per county for ~40 measures: obesity, diabetes, COPD, "
              "coronary heart disease, stroke, depression, high blood pressure, no insurance, "
              "no routine checkup, and more. Crude and age-adjusted; the pipeline keeps both.",
    ),
    "acs": Source(
        key="acs",
        title="ACS 5-year 2023 — county age and poverty",
        publisher="US Census Bureau",
        url="https://api.census.gov/data/2023/acs/acs5?get=NAME,B01002_001E,B17001_001E,B17001_002E,B01001_001E,"
            "B01001_020E,B01001_021E,B01001_022E,B01001_023E,B01001_024E,B01001_025E,"
            "B01001_044E,B01001_045E,B01001_046E,B01001_047E,B01001_048E,B01001_049E&for=county:*",
        local="census_acs5_2023_county.json",
        data_year="2019–2023",
        license="public domain (US federal)",
        landing="https://www.census.gov/data/developers/data-sets/acs-5year.html",
        notes="Median age, share below poverty, share aged 65+. Controls for the models: age "
              "alone explains a large share of morbidity and must be held constant.",
    ),
    "ahrq_systems": Source(
        key="ahrq_systems",
        title="AHRQ Compendium of U.S. Health Systems — hospital linkage file",
        publisher="AHRQ",
        url="https://www.ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/chsp-hospital-linkage-2023.csv",
        local="ahrq_hospital_linkage.csv",
        data_year="2023",
        license="public; cite AHRQ Compendium of U.S. Health Systems",
        landing="https://www.ahrq.gov/chsp/data-resources/compendium-2023.html",
        notes="Maps each hospital (CCN) to its health system and corporate parent. The file "
              "name changes by year; fetch_central tries the candidates in AHRQ_CANDIDATES.",
    ),
    "zcta_county": Source(
        key="zcta_county",
        title="2020 ZCTA to County relationship file",
        publisher="US Census Bureau",
        url="https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt",
        local="census_zcta_county_2020.txt",
        data_year="2020",
        license="public domain (US federal)",
        landing="https://www.census.gov/geographies/reference-files/time-series/geo/relationship-files.html",
        notes="Pipe-delimited. A ZIP that straddles counties is assigned to the county "
              "holding the largest share of its land area.",
    ),
    # --- the code dictionary (pipeline/prices/codes.py) ------------------
    "hcpcs": Source(
        key="hcpcs",
        title="HCPCS Level II quarterly release (alpha-numeric file)",
        publisher="CMS",
        url="https://www.cms.gov/files/zip/2025-hcpcs-alpha-numeric-file-july.zip",
        local="cms_hcpcs_level2.zip",
        data_year="2025",
        license="public domain (US federal); HCPCS Level II is CMS-maintained",
        landing="https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system/quarterly-update",
        notes="Every A–V code with its long description. Gives official text to codes "
              "Medicare did not pay for in the physician file. The file name changes each "
              "quarter; the candidates are tried in order. Optional: without it, those codes "
              "fall back to the hospitals' own wording.",
        candidates=[
            "https://www.cms.gov/files/zip/2025-hcpcs-alpha-numeric-file-july.zip",
            "https://www.cms.gov/files/zip/july-2025-alpha-numeric-hcpcs-file.zip",
            "https://www.cms.gov/files/zip/2025-hcpcs-alpha-numeric-file-april.zip",
            "https://www.cms.gov/files/zip/april-2025-alpha-numeric-hcpcs-file.zip",
            "https://www.cms.gov/files/zip/2025-hcpcs-alpha-numeric-file-january.zip",
            "https://www.cms.gov/files/zip/january-2025-alpha-numeric-hcpcs-file.zip",
        ],
        optional=True,
    ),
    "msdrg": Source(
        key="msdrg",
        title="MS-DRG definitions — IPPS final rule Table 5 (FY2025)",
        publisher="CMS",
        url="https://www.cms.gov/files/zip/fy-2025-ipps-final-rule-table-5.zip",
        local="cms_msdrg_table5.zip",
        data_year="FY2025",
        license="public domain (US federal)",
        landing="https://www.cms.gov/medicare/payment/prospective-payment-systems/acute-inpatient-pps/fy-2025-ipps-final-rule-home-page",
        notes="All ~770 MS-DRGs with titles. Gives a title to DRGs that no hospital had 11+ "
              "Medicare discharges for. Optional, like hcpcs.",
        candidates=[
            "https://www.cms.gov/files/zip/fy-2025-ipps-final-rule-table-5.zip",
            "https://www.cms.gov/files/zip/fy-2025-ipps-final-rule-tables-5.zip",
            "https://www.cms.gov/files/zip/fy-2025-final-rule-table-5.zip",
            "https://www.cms.gov/files/zip/fy-2026-ipps-final-rule-table-5.zip",
        ],
        optional=True,
    ),
}


CKAN = "https://catalog.data.gov/api/3/action/package_show?id={id}"

# AHRQ renames the linkage file each release and its site refuses automated
# fetches from some networks. Try these in order; the first that downloads wins.
AHRQ_CANDIDATES = [
    "https://www.ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/chsp-hospital-linkage-2023.csv",
    "https://www.ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/2023-hospital-linkage-file.csv",
    "https://www.ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/chsp-hospital-linkage-2022.csv",
    "https://www.ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/chsp-hospital-linkage-2021.csv",
]


def discover(src: Source, log=print) -> str:
    """
    Ask catalog.data.gov for the newest CSV matching this source's pattern.
    Returns the URL to use. Falls back to the pin, loudly, on any failure.
    """
    if not src.ckan_id or not src.ckan_match:
        return src.url
    try:
        payload = json.loads(fetch.text(CKAN.format(id=src.ckan_id)))
        resources = payload["result"]["resources"]
    except Exception as e:  # noqa: BLE001
        log(f"  discovery failed for {src.key} ({e}); using pinned URL")
        return src.url

    best_year, best_url = -1, None
    pat = re.compile(src.ckan_match)
    for r in resources:
        url = r.get("url") or ""
        m = pat.search(url)
        if m:
            year = int(m.group(1))
            if year > best_year:
                best_year, best_url = year, url
    if not best_url:
        log(f"  discovery found no matching file for {src.key}; using pinned URL")
        return src.url
    if best_url != src.url:
        log(f"  NOTE: catalog lists a newer file for {src.key} (data year {best_year}): {best_url}")
        log(f"        pinned file is {src.url}")
        log(f"        using the newer one. Re-check column names if the load fails.")
        src.discovered_url = best_url
        return best_url
    return src.url
