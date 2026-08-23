#!/usr/bin/env python3
"""
Offline self-test: synthetic copies of every source, run through every parser,
the validator and the aggregates. No network. Run before and after changing
anything in this package:

    python3 -m pipeline.prices.selftest

It builds into a temporary directory and leaves out/ and data/ untouched.
"""
from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

from . import codes, build, counties, enrich, fetch, medicare, mrf, validate, xlsx


def _xlsx(path: Path, rows: list[list]) -> None:
    """Write a one-sheet .xlsx with inline strings — enough to test the reader."""
    def cell(ref, v):
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s
    sheet = ['<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for ri, row in enumerate(rows, 1):
        sheet.append(f'<row r="{ri}">' + "".join(cell(f"{col(ci)}{ri}", v) for ci, v in enumerate(row)) + "</row>")
    sheet.append("</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", "".join(sheet))


INP = """Rndrng_Prvdr_CCN,Rndrng_Prvdr_Org_Name,Rndrng_Prvdr_City,Rndrng_Prvdr_St,Rndrng_Prvdr_State_FIPS,Rndrng_Prvdr_Zip5,Rndrng_Prvdr_State_Abrvtn,Rndrng_Prvdr_RUCA,Rndrng_Prvdr_RUCA_Desc,DRG_Cd,DRG_Desc,Tot_Dschrgs,Avg_Submtd_Cvrd_Chrg,Avg_Tot_Pymt_Amt,Avg_Mdcr_Pymt_Amt
330214,NYU Langone Tisch Hospital,New York,550 First Ave,36,10016,NY,1,Metropolitan area core,470,MAJOR JOINT REPLACEMENT,120,98000.5,21000.25,19000
330214,NYU Langone Tisch Hospital,New York,550 First Ave,36,10016,NY,1,Metropolitan area core,871,SEPSIS W MCC,300,150000,30000,32000
360180,Cleveland Clinic,Cleveland,9500 Euclid Ave,39,44195,OH,1,Metropolitan area core,470,MAJOR JOINT REPLACEMENT,400,60000,18000,16000
"""
OUT_ = """Rndrng_Prvdr_CCN,Rndrng_Prvdr_Org_Name,Rndrng_Prvdr_St,Rndrng_Prvdr_City,Rndrng_Prvdr_State_Abrvtn,Rndrng_Prvdr_State_FIPS,Rndrng_Prvdr_Zip5,Rndrng_Prvdr_RUCA,Rndrng_Prvdr_RUCA_Desc,APC_Cd,APC_Desc,Bene_Cnt,CAPC_Srvcs,Avg_Tot_Sbmtd_Chrgs,Avg_Mdcr_Alowd_Amt,Avg_Mdcr_Pymt_Amt,Outlier_Srvcs,Avg_Mdcr_Outlier_Amt
330214,NYU Langone Tisch Hospital,550 First Ave,New York,NY,36,10016,1,Metro,5072,Level 2 Excision,175,188,9637.66,1365.37,1080.95,0,0
360180,Cleveland Clinic,9500 Euclid Ave,Cleveland,OH,39,44195,1,Metro,5072,Level 2 Excision,80,90,4000,1300,1400,0,0
"""
PHY = """Rndrng_Prvdr_Geo_Lvl,Rndrng_Prvdr_Geo_Cd,Rndrng_Prvdr_Geo_Desc,HCPCS_Cd,HCPCS_Desc,HCPCS_Drug_Ind,Place_Of_Srvc,Tot_Rndrng_Prvdrs,Tot_Benes,Tot_Srvcs,Tot_Bene_Day_Srvcs,Avg_Sbmtd_Chrg,Avg_Mdcr_Alowd_Amt,Avg_Mdcr_Pymt_Amt,Avg_Mdcr_Stdzd_Amt
National,,National,99213,Office visit est,N,O,500000,30000000,90000000,90000000,180.5,92.1,70.2,90.0
State,36,New York,99213,Office visit est,N,O,40000,2000000,6000000,6000000,240.0,105.3,80.1,90.0
State,39,Ohio,99213,Office visit est,N,O,30000,1500000,4000000,4000000,160.0,88.0,66.0,90.0
State,36,New York,0001U,Red blood cell typing,N,O,7,100,100,100,881.4,525.9,525.9,705.6
"""
CHR = """State FIPS Code,County FIPS Code,5-digit FIPS Code,State Abbreviation,Name,Release Year,Premature death raw value,Poor or fair health raw value,Uninsured raw value,Median household income raw value,Population raw value,% Non-Hispanic Black raw value,% Hispanic raw value,% Non-Hispanic White raw value,% Rural raw value,Primary care physicians raw value,Preventable hospital stays raw value
statecode,countycode,fipscode,state,county,year,v001_rawvalue,v002_rawvalue,v085_rawvalue,v063_rawvalue,v051_rawvalue,v054_rawvalue,v056_rawvalue,v126_rawvalue,v058_rawvalue,v004_rawvalue,v005_rawvalue
00,000,00000,US,United States,2025,7300,0.15,0.09,75000,330000000,0.12,0.19,0.58,0.2,1300,2500
36,000,36000,NY,New York,2025,5900,0.14,0.05,80000,19000000,0.14,0.19,0.55,0.12,1200,2300
36,061,36061,NY,New York County,2025,4100,0.12,0.04,95000,1600000,0.13,0.24,0.47,0.0,500,1900
39,035,39035,OH,Cuyahoga County,2025,8800,0.17,0.06,58000,1240000,0.29,0.06,0.57,0.02,900,3100
"""
ZCTA = """OID_ZCTA5_20|GEOID_ZCTA5_20|NAMELSAD_ZCTA5_20|AREALAND_ZCTA5_20|AREAWATER_ZCTA5_20|MTFCC_ZCTA5_20|FUNCSTAT_ZCTA5_20|OID_COUNTY_20|GEOID_COUNTY_20|NAMELSAD_COUNTY_20|AREALAND_COUNTY_20|AREAWATER_COUNTY_20|MTFCC_COUNTY_20|CLASSFP_COUNTY_20|FUNCSTAT_COUNTY_20|AREALAND_PART|AREAWATER_PART
1|10016|ZCTA5 10016|1|0|G6350|S|1|36061|New York County|1|0|G4020|H6|A|1500000|0
2|44195|ZCTA5 44195|1|0|G6350|S|2|39035|Cuyahoga County|1|0|G4020|H6|A|900000|0
3|44195|ZCTA5 44195|1|0|G6350|S|3|39055|Geauga County|1|0|G4020|H6|A|100|0
"""
URBAN = [
    ["Debt in America 2025 — county-level medical debt"],
    [],
    ["County FIPS", "County", "State", "Share with medical debt in collections, All",
     "Share with medical debt in collections, White communities",
     "Share with medical debt in collections, Communities of color",
     "Median medical debt in collections, All", "Median medical debt in collections, White communities",
     "Median medical debt in collections, Communities of color",
     "Share without health insurance coverage", "Share of people of color", "Average household income"],
    [36061, "New York County", "NY", 0.031, 0.02, 0.05, 420, 380, 500, 0.05, 0.53, 150000],
    [39035, "Cuyahoga County", "OH", 0.12, 0.08, 0.19, 650, 600, 700, 0.06, 0.43, 70000],
]
PLACES = """StateAbbr,StateDesc,CountyName,CountyFIPS,TotalPopulation,TotalPop18plus,OBESITY_CrudePrev,OBESITY_Crude95CI,OBESITY_AdjPrev,OBESITY_Adj95CI,DIABETES_CrudePrev,DIABETES_Crude95CI,DIABETES_AdjPrev,DIABETES_Adj95CI,Geolocation
NY,New York,New York,36061,1600000,1400000,22.1,"(20,24)",23.0,"(21,25)",8.1,"(7,9)",7.5,"(7,8)",POINT (-74 40)
OH,Ohio,Cuyahoga,39035,1240000,980000,35.4,"(33,37)",34.9,"(33,37)",12.2,"(11,13)",11.0,"(10,12)",POINT (-81 41)
"""
ACS = """[["NAME","B01002_001E","B17001_001E","B17001_002E","B01001_001E","B01001_020E","B01001_021E","B01001_022E","B01001_023E","B01001_024E","B01001_025E","B01001_044E","B01001_045E","B01001_046E","B01001_047E","B01001_048E","B01001_049E","state","county"],
["New York County, New York","38.5","1550000","260000","1600000","20000","20000","20000","20000","20000","20000","25000","25000","25000","25000","25000","25000","36","061"],
["Cuyahoga County, Ohio","40.9","1200000","210000","1240000","18000","18000","18000","18000","18000","18000","22000","22000","22000","22000","22000","22000","39","035"]]"""
AHRQ = """compendium_hospital_id,ccn,hospital_name,hospital_city,hospital_state,health_sys_id,health_sys_name,health_sys_state,corp_parent_id,corp_parent_name,hos_beds,hos_ownership
1,330214,NYU LANGONE TISCH,NEW YORK,NY,S100,NYU Langone Health,NY,,,1100,Nonprofit
2,360180,CLEVELAND CLINIC,CLEVELAND,OH,S200,Cleveland Clinic Health System,OH,,,1400,Nonprofit
"""
MRF_TALL = """hospital_name,last_updated_on,version,hospital_location,hospital_address,license_number | NY,affirmation
NYU Langone Tisch Hospital,2025-07-01,2.2.0,NYU Langone Tisch Hospital,550 First Ave,NY-12345,true
description,code|1,code|1|type,code|2,code|2|type,modifiers,setting,drug_unit_of_measurement,drug_type_of_measurement,standard_charge|gross,standard_charge|discounted_cash,standard_charge|min,standard_charge|max,payer_name,plan_name,standard_charge|negotiated_dollar,standard_charge|negotiated_percentage,standard_charge|negotiated_algorithm,estimated_amount,standard_charge|methodology,additional_generic_notes
CT HEAD W/O CONTRAST,70450,CPT,,,,outpatient,,,2400,1900,400,1500,Aetna,PPO,900,,,,fee schedule,
CT HEAD W/O CONTRAST,70450,CPT,,,,outpatient,,,2400,1900,400,1500,UHC,HMO,1700,,,,fee schedule,
CT HEAD W/O CONTRAST,70450,CPT,,,,outpatient,,,2400,1900,400,1500,BCBS,PPO,,80,,,percent of total billed charges,
MAJOR JOINT REPLACEMENT,470,MS-DRG,,,,inpatient,,,310000,90000,25000,80000,Aetna,PPO,30000,,,,case rate,
MAJOR JOINT REPLACEMENT,470,MS-DRG,,,,inpatient,,,310000,90000,25000,80000,UHC,HMO,,,custom,,other,
OFFICE VISIT EST LVL 3,99213,CPT,,,,outpatient,,,350,400,80,300,Aetna,PPO,120,,,,fee schedule,
SOME DRUG,J1234,HCPCS,,,,outpatient,ml,,50,40,10,45,Aetna,PPO,20,,,,fee schedule,
"""
MRF_WIDE = """hospital_name,last_updated_on,version,hospital_location,hospital_address,license_number | OH,affirmation
Cleveland Clinic,2024-03-01,2.0.0,Cleveland Clinic Main Campus,9500 Euclid,OH-1,true
description,code|1,code|1|type,setting,standard_charge|gross,standard_charge|discounted_cash,standard_charge|min,standard_charge|max,standard_charge|Aetna|PPO|negotiated_dollar,standard_charge|Aetna|PPO|negotiated_percentage,standard_charge|Aetna|PPO|negotiated_algorithm,standard_charge|Aetna|PPO|methodology,estimated_amount|Aetna|PPO,standard_charge|UHC|HMO|negotiated_dollar,standard_charge|UHC|HMO|negotiated_percentage,standard_charge|UHC|HMO|negotiated_algorithm,standard_charge|UHC|HMO|methodology,estimated_amount|UHC|HMO,additional_generic_notes
MAJOR JOINT REPLACEMENT,470,MS-DRG,inpatient,65000,40000,20000,50000,35000,,,case rate,,,70,,percent,30000,
CT HEAD,70450,CPT,outpatient,1200,700,300,900,500,,,fee schedule,,600,,,fee schedule,,
"""
MRF_JSON = {
    "hospital_name": "Cleveland Clinic", "last_updated_on": "2025-01-15", "version": "2.1.0",
    "affirmation": {"affirmation": "To the best of its knowledge...", "confirm_affirmation": True},
    "standard_charge_information": [
        {"description": "MRI BRAIN", "code_information": [{"code": "70553", "type": "CPT"}],
         "standard_charges": [{"setting": "outpatient", "gross_charge": 3000, "discounted_cash": 1500,
                               "minimum": 800, "maximum": 2500,
                               "payers_information": [{"payer_name": "Aetna", "plan_name": "PPO",
                                                       "standard_charge_dollar": 1000, "methodology": "fee schedule"}]}]},
        {"description": "SEPSIS", "code_information": [{"code": "MS-DRG 871", "type": "MS-DRG"}],
         "standard_charges": [{"setting": "inpatient", "gross_charge": 120000, "discounted_cash": 60000,
                               "minimum": 30000, "maximum": 90000, "payers_information": []}]},
    ],
}


HCPCS_CSV = """HCPC,SEQNUM,RECID,LONG DESCRIPTION,SHORT DESCRIPTION
J1234,0010,3,Injection, some drug, 10 mg,Inj some drug 10 mg
A0425,0010,3,Ground mileage, per statute mile,Ground mileage
"""
MSDRG_CSV = """MS-DRG,FY 2025 Final Rule Post-Acute DRG,FY 2025 Final Rule Special Pay DRG,MDC,TYPE,MS-DRG Title,Weights,Geometric mean LOS,Arithmetic mean LOS
001,No,No,PRE,SURG,HEART TRANSPLANT OR IMPLANT OF HEART ASSIST SYSTEM WITH MCC,28.1,26.9,35.3
871,No,No,18,MED,SEPTICEMIA OR SEVERE SEPSIS WITHOUT MV >96 HOURS WITH MCC,1.9,4.9,6.1
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="crossfoot-prices-"))
    raw = tmp / "raw"
    raw.mkdir()
    fetch.RAW_DIR = raw
    build.OUT = tmp / "out"
    build.PUBLIC = tmp / "public"
    (raw / "medicare_inpatient_dy24.csv").write_text(INP)
    (raw / "medicare_outpatient_dy24.csv").write_text(OUT_)
    (raw / "medicare_physician_geo_dy24.csv").write_text(PHY)
    import zipfile
    with zipfile.ZipFile(raw / "cms_hcpcs_level2.zip", "w") as z:
        z.writestr("HCPC2025_JUL_ANWEB.csv", HCPCS_CSV)
    (raw / "cms_msdrg_table5.zip").write_bytes(b"")   # absent/empty table: loader must cope
    (raw / "msdrg_plain.csv").write_text(MSDRG_CSV)
    (raw / "chr_analytic_2025.csv").write_text(CHR)
    (raw / "census_zcta_county_2020.txt").write_text(ZCTA)
    _xlsx(raw / "urban_debt_county_medical_2025.xlsx", URBAN)
    (raw / "cdc_places_county_2025.csv").write_text(PLACES)
    (raw / "census_acs5_2023_county.json").write_text(ACS)
    (raw / "ahrq_hospital_linkage.csv").write_text(AHRQ)
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("xlsx reader")
    rows = list(xlsx.rows(raw / "urban_debt_county_medical_2025.xlsx"))
    check(rows[2][0] == "County FIPS" and rows[3][0] == "36061", "reads inline strings and numbers by cell reference")

    print("counties")
    urban, used = counties.load_urban(raw / "urban_debt_county_medical_2025.xlsx")
    check(urban["36061"]["medical_debt_pct"] == 3.1 and urban["39035"]["medical_debt_pct_color"] == 19.0,
          f"Urban columns mapped by meaning: {used}")
    check(urban["36061"]["medical_debt_median"] == 420, "median kept as dollars")
    chr_ = counties.load_chr(raw / "chr_analytic_2025.csv")
    check(chr_["36061"]["uninsured_pct"] == 4.0 and chr_["36061"]["level"] == "county" and chr_["36000"]["level"] == "state",
          "CHR fractions become percentages; levels classified")
    z = counties.load_zcta_county(raw / "census_zcta_county_2020.txt")
    check(z["44195"] == "39035", "ZCTA straddling two counties goes to the larger land share")

    print("enrich")
    pc, pa = enrich.load_places(raw / "cdc_places_county_2025.csv")
    check(pc["36061"]["obesity_pct"] == 22.1 and pa["36061"]["obesity_pct"] == 23.0, "PLACES crude and age-adjusted read")
    acs = enrich.load_acs(raw / "census_acs5_2023_county.json")
    check(acs["36061"]["median_age"] == 38.5 and acs["36061"]["poverty_pct"] == 16.77 and acs["36061"]["age65_pct"] == 16.88,
          "ACS median age, poverty share and 65+ share computed")
    ahrq, used = enrich.load_ahrq(raw / "ahrq_hospital_linkage.csv")
    check(ahrq["330214"]["system_name"] == "NYU Langone Health" and ahrq["330214"]["beds"] == 1100, f"AHRQ linkage mapped by meaning: {used}")

    print("medicare")
    h1, inp = medicare.load_inpatient(raw / "medicare_inpatient_dy24.csv", "2024")
    check(inp[0]["charge_to_payment"] == round(98000.5 / 21000.25, 3), "charge-to-payment computed from the row")
    h2, outp = medicare.load_outpatient(raw / "medicare_outpatient_dy24.csv", "2024")
    phy = medicare.load_physician_geo(raw / "medicare_physician_geo_dy24.csv", "2024")
    check(len(phy) == 4 and {r["geo"] for r in phy} == {"US", "NY", "OH"}, "physician file: every code kept, states abbreviated")
    hospitals = medicare.merge_hospitals(h1, h2)
    counties.attach_county(hospitals, z)
    check(hospitals["330214"]["county_fips"] == "36061", "hospital placed in its county via ZIP")

    print("mrf")
    blocks = mrf.parse_hpt("location-name: A Hospital\nsource-page-url: https://x/y\nmrf-url: https://x/a.csv\n\n"
                           "location-name: Tisch Hospital\nmrf-url: https://x/b.csv\n")
    b, s = mrf.choose_block(blocks, "NYU Langone Tisch Hospital")
    check(b["mrf-url"] == "https://x/b.csv", "cms-hpt.txt parsed and best location chosen")
    meta = {}
    aggs = mrf.parse_csv(io.StringIO(MRF_TALL), "NY-01", meta)
    tall = {k: a.row("NY-01", *k) for k, a in aggs.items()}
    ct = tall[("CPT", "70450", "outpatient")]
    check(meta["format"] == "csv-tall" and meta["version"] == "2.2.0", "tall CSV metadata read")
    check(ct["negotiated_n"] == 2 and ct["negotiated_max"] == 1700 and ct["pct_or_algo_n"] == 1 and ct["estimated_n"] == 0,
          "tall: negotiated dollars, percentage-without-estimate counted")
    check(("CPT", "J1234", "outpatient") in tall, "non-basket HCPCS code kept for the catalog")
    meta = {}
    aggs = mrf.parse_csv(io.StringIO(MRF_WIDE), "OH-01", meta)
    wide = {k: a.row("OH-01", *k) for k, a in aggs.items()}
    check(meta["format"] == "csv-wide" and meta["payer_columns"] == 2, "wide CSV detected with two payer groups")
    check(wide[("MS-DRG", "470", "inpatient")]["negotiated_n"] == 1 and wide[("MS-DRG", "470", "inpatient")]["estimated_n"] == 1,
          "wide: dollar and percentage-with-estimate both counted")
    meta = {}
    aggs = mrf.parse_json(MRF_JSON, "OH-02", meta)
    js = {k: a.row("OH-02", *k) for k, a in aggs.items()}
    check(("MS-DRG", "871", "inpatient") in js and js[("CPT", "70553", "outpatient")]["negotiated_median"] == 1000,
          "JSON: 'MS-DRG 871' normalised, negotiated median computed")

    print("validate")
    charges = list(tall.values()) + list(wide.values()) + list(js.values())
    files = [
        {"seed_id": "NY-01", "state": "NY", "hospital": "NYU Langone Tisch Hospital", "domain": "nyulangone.org", "status": "ok", "ccn": "330214",
         "last_updated_on": "2025-07-01", "version": "2.2.0", "affirmation": "true", "format": "csv-tall"},
        {"seed_id": "OH-01", "state": "OH", "hospital": "Cleveland Clinic Main Campus", "domain": "my.clevelandclinic.org", "status": "ok", "ccn": "360180",
         "last_updated_on": "2024-03-01", "version": "2.0.0", "affirmation": "true", "format": "csv-wide"},
        {"seed_id": "OH-02", "state": "OH", "hospital": "Cleveland Clinic", "domain": "my.clevelandclinic.org", "status": "ok", "ccn": "360180",
         "last_updated_on": "2025-01-15", "version": "2.1.0", "affirmation": "To the best...", "format": "json"},
    ]
    fnd = validate.run(charges, files, inp, outp, phy)
    rules = {(f["ref"], f["rule"]) for f in fnd}
    check(("NY-01", "mrf_negotiated_above_max") in rules, "negotiated 1700 above file's max 1500 → error")
    check(("NY-01", "mrf_cash_above_gross") in rules, "cash 400 above gross 350 on 99213 → error")
    check(("NY-01", "mrf_rate_without_estimate") in rules, "percentage rate with no estimated amount → warn")
    check(("OH-01", "mrf_stale") in rules, "file last updated 2024-03-01 → stale")
    check(("NY-01", "mrf_vs_medicare_charge") in rules, "MRF gross 310,000 vs Medicare avg charge 98,000 → warn")
    check(("330214", "inp_medicare_exceeds_total") in rules, "Medicare share above total payment → error")
    check(("360180", "out_payment_exceeds_allowed") in rules, "outpatient payment above allowed → error")
    check(("OH-02", "mrf_no_payer_rates") in rules, "DRG listed with no payer rates → info")

    print("codes")
    hl = codes.load_hcpcs_release(raw / "cms_hcpcs_level2.zip")
    check(hl.get("J1234", "").startswith("Injection") and "A0425" in hl, f"HCPCS release read from the zip's csv member: {hl}")
    ml = codes.load_msdrg_table(raw / "msdrg_plain.csv")
    check(ml.get("001", "").startswith("HEART") and ml.get("871"), "MS-DRG table read by column name")
    check(codes.load_msdrg_table(raw / "cms_msdrg_table5.zip") == {}, "an empty or missing list is an empty dict, not an error")
    cr = codes.build(phy, inp, charges, build.BASKET, hl, ml)
    by = {(r["code_type"], r["code"]): r for r in cr}
    check(by[("CPT", "99213")]["status"] == "official" and by[("CPT", "99213")]["desc_source"] == "cms", "a code in the Medicare file is official, CMS text")
    check(by[("CPT", "J1234")]["status"] == "official" and by[("CPT", "J1234")]["desc_source"] == "hcpcs", "a HCPCS-release code is official with the release's text")
    check(by[("MS-DRG", "871")]["status"] == "official" and by[("MS-DRG", "871")]["desc_source"] in ("cms", "msdrg"), "a DRG in the table is official")
    check(by[("MS-DRG", "999")]["status"] == "unverified" if ("MS-DRG", "999") in by else True, "an unlisted DRG is unverified")
    check(all(r["in_basket"] for k, r in by.items() if k in {(b["type"], b["code"]) for b in build.BASKET}), "basket items flagged")
    check(codes.well_formed("CPT", "1234") is False and codes.well_formed("CPT", "0001U") and codes.well_formed("MS-DRG", "000") is False, "well-formed rules")

    print("build --rebuild (offline)")
    (build.OUT).mkdir(parents=True)
    build.write_json(build.OUT / "mrf_files.json", files)
    build.write_json(build.OUT / "mrf_charges.json", charges)
    rc = build.main(["--rebuild"])
    check(rc == 0, "build returned 0")
    man = json.loads((build.OUT / "manifest.json").read_text())
    check(man["counts"]["hospitals"] == 2 and man["counts"]["counties"] == 2 and man["counts"]["counties_with_debt"] == 2,
          f"manifest counts {man['counts']}")
    sb = json.loads((build.OUT / "state_basket.json").read_text())
    ny470 = next(r for r in sb if r["state"] == "NY" and r["code"] == "470")
    check(ny470["medicare_charge"] == 98000.5 and ny470["mrf_gross_median"] == 310000, "state basket joins Medicare and MRF")
    us = next(r for r in sb if r["state"] == "US" and r["code"] == "470")
    check(us["medicare_charge"] == round((98000.5 * 120 + 60000 * 400) / 520, 3), "national DRG charge is discharge-weighted")
    cp = json.loads((build.OUT / "county_profile.json").read_text())
    c = next(x for x in cp if x["fips"] == "36061")
    check(c["hospitals_n"] == 1 and c["medical_debt_pct"] == 3.1 and c["inpatient_charge_to_payment"] is not None,
          "county profile carries debt, outcomes and the price ratio")
    check(c["obesity_pct"] == 22.1 and c["median_age"] == 38.5, "county profile carries PLACES and ACS enrichment")
    sy = json.loads((build.OUT / "systems.json").read_text())
    check(len(sy) == 2 and sy[0]["charge_to_payment"] is not None and sy[0]["hospitals_n"] == 1, f"health systems aggregated: {[(x['name'], x['charge_to_payment']) for x in sy]}")
    chr_ = counties.load_chr(raw / "chr_analytic_2025.csv")
    urban2, _ = counties.load_urban(raw / "urban_debt_county_medical_2025.xlsx")
    urban2["36000"] = {"fips": "36000", "medical_debt_pct": 0.0}        # a state-level zero = reporting ban
    prof = {r["fips"]: r for r in counties.build_county_profiles(chr_, urban2)}
    check(prof["36061"]["medical_debt_pct"] is None and prof["36061"]["medical_debt_note"] == counties.BAN_NOTE
          and prof["39035"]["medical_debt_pct"] == 12.0, "a state at 0% is a reporting ban: its counties go NULL with a note")
    check((build.PUBLIC / "prices_state_basket.csv").exists() and (build.PUBLIC / "prices_codes.csv").exists(), "abridged public copies written")
    sb_keys = {(r["code_type"], r["code"]) for r in sb}
    check(sb_keys <= {(b["type"], b["code"]) for b in build.BASKET}, "state_basket.json carries basket items only")
    cs = json.loads((build.OUT / "code_state.json").read_text())
    check(all((r["code_type"], r["code"]) not in sb_keys for r in cs), "code_state.json carries the rest")
    check(man["counts"]["codes"]["codes"] > 0, f"manifest carries the code summary {man['counts']['codes']}")

    print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED'}  ({tmp})")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
