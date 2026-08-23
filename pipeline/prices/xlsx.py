"""
A minimal .xlsx reader — standard library only.

An .xlsx file is a zip of XML. This reads the shared-strings table and the
first worksheet (or a named one) and yields rows as lists of strings. It
handles inline strings, shared strings, numbers and booleans, and nothing
else — which is all a published statistics workbook contains. Formulas are
read as their cached value. Dates come back as Excel serial numbers.

Not a general reader. Enough for Urban Institute's Debt in America files.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator, Optional

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _col_index(ref: str) -> int:
    """'A1' -> 0, 'AB7' -> 27."""
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        return [s.get("name") for s in wb.find("m:sheets", NS)]


def rows(path: Path, sheet: Optional[str] = None) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sst.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))

        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_target = {r.get("Id"): r.get("Target") for r in rels}
        target = None
        for s in wb.find("m:sheets", NS):
            if sheet is None or s.get("name") == sheet:
                rid = s.get(f"{{{NS['r']}}}id")
                target = rel_target[rid]
                break
        if target is None:
            raise KeyError(f"sheet {sheet!r} not found in {path.name}")
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target

        root = ET.fromstring(z.read(target))
        for row in root.iter(f"{{{NS['m']}}}row"):
            out: list[str] = []
            for c in row.findall("m:c", NS):
                idx = _col_index(c.get("r", "A"))
                while len(out) < idx:
                    out.append("")
                t = c.get("t")
                v = c.find("m:v", NS)
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    is_ = c.find("m:is", NS)
                    val = "".join(x.text or "" for x in is_.iter(f"{{{NS['m']}}}t")) if is_ is not None else ""
                elif t == "b" and v is not None:
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text if v is not None and v.text is not None else ""
                out.append(val)
            yield out
