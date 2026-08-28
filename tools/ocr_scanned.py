"""
Read the disclosures that were published as pictures.

A handful of payers post the mandated report as a scanned or image-only PDF --
no text layer at all, so docs.py stores the bytes and renders nothing, and the
document drops out of the dataset without ever being counted as a failure. This
recovers those, using the OCR that ships with macOS (Vision, via
tools/ocr_page.swift) so nothing new has to be installed.

OCR is not trusted on its own. Every figure it produces still goes through the
same reconciliation the typed documents do -- approved + denied against the
payer's own printed total -- and a row that fails is held back rather than
published. An OCR misread of a digit breaks that identity, so the check is a
real test of the reading and not a formality.

Documents recovered this way are stamped text_source="ocr" in SOURCE.json, so a
figure that came from a picture can always be told apart from one that came from
text the payer published.

    python3 tools/ocr_scanned.py            # report what is missing text
    python3 tools/ocr_scanned.py --write    # OCR them and store the text
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "raw" / "preauth" / "docs"
SRC = ROOT / "tools" / "ocr_page.swift"
BIN = ROOT / "tools" / ".ocr_page"


def build() -> Path:
    """Compile the Vision helper once, next to its source."""
    if BIN.exists() and BIN.stat().st_mtime >= SRC.stat().st_mtime:
        return BIN
    print(f"compiling {SRC.relative_to(ROOT)} -> {BIN.relative_to(ROOT)}")
    subprocess.run(["swiftc", "-O", "-o", str(BIN), str(SRC)], check=True)
    return BIN


def scanned() -> list[Path]:
    """Document directories holding a PDF that produced no text."""
    out = []
    for meta_path in sorted(DOCS.glob("*/SOURCE.json")):
        folder = meta_path.parent
        if (folder / "text.txt").exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            continue
        if meta.get("error"):
            continue
        pdfs = [p for p in folder.iterdir() if p.suffix.lower() == ".pdf"]
        if pdfs:
            out.append(folder)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="store the recovered text")
    args = ap.parse_args()

    folders = scanned()
    print(f"{len(folders)} stored PDFs produced no text")
    if not folders:
        return 0
    if not args.write:
        for f in folders:
            print(f"  {json.loads((f / 'SOURCE.json').read_text()).get('url', f.name)}")
        print("\nre-run with --write to OCR them")
        return 0

    ocr = build()
    recovered = 0
    for folder in folders:
        meta_path = folder / "SOURCE.json"
        meta = json.loads(meta_path.read_text())
        pdf = next(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")
        try:
            done = subprocess.run([str(ocr), str(pdf)], capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            print(f"  timeout  {meta.get('url', folder.name)[:88]}")
            continue
        text = done.stdout.decode("utf-8", "replace").strip()
        if len(text) < 200:
            print(f"  no text  {meta.get('url', folder.name)[:88]}")
            continue
        (folder / "text.txt").write_text(text + "\n")
        meta["text_source"] = "ocr"
        meta_path.write_text(json.dumps(meta, indent=1) + "\n")
        recovered += 1
        print(f"  {len(text):>7} chars  {meta.get('url', folder.name)[:80]}")
    print(f"\n{recovered} documents recovered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
