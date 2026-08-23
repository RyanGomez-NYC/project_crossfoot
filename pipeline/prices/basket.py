"""
The basket: a fixed set of services compared across every source.

Hospital price files list tens of thousands of items, most of them drugs and
supplies that no two hospitals describe the same way. A comparison across
hospitals needs a fixed list of things that are the same thing everywhere. CMS
named 70 "shoppable services" for exactly this purpose; the basket below is
the subset of them that are common, high-volume and easily recognised, plus
the office and emergency visit codes that are the closest public proxy for an
urgent-care price, and ten MS-DRGs that between them cover a large share of
inpatient admissions.

Three code systems:
    CPT     a procedure or visit, billed by a hospital outpatient department or a clinician
    MS-DRG  an inpatient admission, as Medicare pays for it
    (APCs, Medicare's outpatient payment groups, are kept in full from the
     outpatient file and are not part of the basket — they do not appear in
     hospital price files.)

`group` is the analysis grouping; `urgent` marks the codes that stand in for an
urgent-care visit.
"""
from __future__ import annotations

BASKET: list[dict] = [
    # --- visits: the urgent-care proxy ---------------------------------------
    {"code": "99203", "type": "CPT", "group": "visit", "urgent": True,
     "label": "New patient office visit, low complexity"},
    {"code": "99204", "type": "CPT", "group": "visit", "urgent": True,
     "label": "New patient office visit, moderate complexity"},
    {"code": "99213", "type": "CPT", "group": "visit", "urgent": True,
     "label": "Established patient office visit, low complexity"},
    {"code": "99214", "type": "CPT", "group": "visit", "urgent": True,
     "label": "Established patient office visit, moderate complexity"},
    {"code": "99283", "type": "CPT", "group": "visit", "urgent": True,
     "label": "Emergency department visit, low-moderate"},
    {"code": "99284", "type": "CPT", "group": "visit", "urgent": True,
     "label": "Emergency department visit, moderate-high"},
    {"code": "99285", "type": "CPT", "group": "visit", "urgent": True,
     "label": "Emergency department visit, high complexity"},
    # --- labs -------------------------------------------------------------
    {"code": "80053", "type": "CPT", "group": "lab", "label": "Comprehensive metabolic panel"},
    {"code": "80061", "type": "CPT", "group": "lab", "label": "Lipid panel"},
    {"code": "85025", "type": "CPT", "group": "lab", "label": "Complete blood count with differential"},
    {"code": "84443", "type": "CPT", "group": "lab", "label": "Thyroid stimulating hormone"},
    {"code": "81001", "type": "CPT", "group": "lab", "label": "Urinalysis, automated with microscopy"},
    {"code": "36415", "type": "CPT", "group": "lab", "label": "Blood draw (venipuncture)"},
    # --- imaging ----------------------------------------------------------
    {"code": "71046", "type": "CPT", "group": "imaging", "label": "Chest x-ray, 2 views"},
    {"code": "73630", "type": "CPT", "group": "imaging", "label": "Foot x-ray"},
    {"code": "70450", "type": "CPT", "group": "imaging", "label": "CT head without contrast"},
    {"code": "74177", "type": "CPT", "group": "imaging", "label": "CT abdomen and pelvis with contrast"},
    {"code": "70553", "type": "CPT", "group": "imaging", "label": "MRI brain with and without contrast"},
    {"code": "72148", "type": "CPT", "group": "imaging", "label": "MRI lumbar spine without contrast"},
    {"code": "73721", "type": "CPT", "group": "imaging", "label": "MRI knee without contrast"},
    {"code": "76700", "type": "CPT", "group": "imaging", "label": "Ultrasound abdomen, complete"},
    {"code": "77067", "type": "CPT", "group": "imaging", "label": "Screening mammogram, bilateral"},
    {"code": "93000", "type": "CPT", "group": "imaging", "label": "Electrocardiogram with interpretation"},
    {"code": "93306", "type": "CPT", "group": "imaging", "label": "Echocardiogram, complete"},
    # --- procedures -------------------------------------------------------
    {"code": "45378", "type": "CPT", "group": "procedure", "label": "Colonoscopy, diagnostic"},
    {"code": "45380", "type": "CPT", "group": "procedure", "label": "Colonoscopy with biopsy"},
    {"code": "43239", "type": "CPT", "group": "procedure", "label": "Upper GI endoscopy with biopsy"},
    {"code": "47562", "type": "CPT", "group": "procedure", "label": "Laparoscopic gallbladder removal"},
    {"code": "49505", "type": "CPT", "group": "procedure", "label": "Inguinal hernia repair, age 5+"},
    {"code": "29881", "type": "CPT", "group": "procedure", "label": "Knee arthroscopy with meniscectomy"},
    {"code": "19120", "type": "CPT", "group": "procedure", "label": "Breast lesion excision"},
    {"code": "66984", "type": "CPT", "group": "procedure", "label": "Cataract removal with lens insert"},
    {"code": "62323", "type": "CPT", "group": "procedure", "label": "Lumbar epidural injection"},
    {"code": "59400", "type": "CPT", "group": "procedure", "label": "Vaginal delivery, global care"},
    {"code": "59510", "type": "CPT", "group": "procedure", "label": "Cesarean delivery, global care"},
    {"code": "27447", "type": "CPT", "group": "procedure", "label": "Total knee replacement"},
    {"code": "27130", "type": "CPT", "group": "procedure", "label": "Total hip replacement"},
    # --- inpatient admissions (MS-DRG) -------------------------------------
    {"code": "470", "type": "MS-DRG", "group": "inpatient", "label": "Major joint replacement of lower extremity without MCC"},
    {"code": "871", "type": "MS-DRG", "group": "inpatient", "label": "Sepsis without MV >96 hours with MCC"},
    {"code": "872", "type": "MS-DRG", "group": "inpatient", "label": "Sepsis without MV >96 hours without MCC"},
    {"code": "291", "type": "MS-DRG", "group": "inpatient", "label": "Heart failure and shock with MCC"},
    {"code": "392", "type": "MS-DRG", "group": "inpatient", "label": "Esophagitis, gastroenteritis and misc digestive disorders without MCC"},
    {"code": "194", "type": "MS-DRG", "group": "inpatient", "label": "Simple pneumonia and pleurisy with CC"},
    {"code": "690", "type": "MS-DRG", "group": "inpatient", "label": "Kidney and urinary tract infections without MCC"},
    {"code": "807", "type": "MS-DRG", "group": "inpatient", "label": "Vaginal delivery without sterilization or D&C without CC/MCC"},
    {"code": "788", "type": "MS-DRG", "group": "inpatient", "label": "Cesarean section without sterilization without CC/MCC"},
    {"code": "603", "type": "MS-DRG", "group": "inpatient", "label": "Cellulitis without MCC"},
    {"code": "177", "type": "MS-DRG", "group": "inpatient", "label": "Respiratory infections and inflammations with MCC"},
    {"code": "247", "type": "MS-DRG", "group": "inpatient", "label": "Percutaneous cardiovascular procedure with drug-eluting stent without MCC"},
]

BY_KEY: dict[tuple[str, str], dict] = {(b["type"], b["code"]): b for b in BASKET}
CPT_CODES: frozenset[str] = frozenset(b["code"] for b in BASKET if b["type"] == "CPT")
DRG_CODES: frozenset[str] = frozenset(b["code"] for b in BASKET if b["type"] == "MS-DRG")
URGENT_CODES: frozenset[str] = frozenset(b["code"] for b in BASKET if b.get("urgent"))


def lookup(code_type: str, code: str) -> dict | None:
    return BY_KEY.get((code_type, code))
