"""
Builds docs/report/JeevaFlow_Security_Features_Report.pdf from the
Markdown master.

    BUILD_DEPS=<dir with node_modules/{marked,playwright-core}> \
    python build_pdf.py

Pass 1 renders the PDF and locates every section heading; pass 2
re-renders with real TOC page numbers. Headers and footers are then
stamped on every page except the cover.
"""

import html
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
REPORT = HERE.parent
MD = REPORT / "JeevaFlow_Security_Features_Report.md"
PDF = REPORT / "JeevaFlow_Security_Features_Report.pdf"

TEAL = (15 / 255, 118 / 255, 110 / 255)
MUTED = (71 / 255, 85 / 255, 105 / 255)

HEADER_LEFT = "JEEVAFLOW  ·  Healthcare Data Security & Clinical Workflow Architecture Report"
HEADER_RIGHT = "Synthetic data only"
FOOTER_LEFT = "Team Geek  ·  2 October 2026  ·  Development environment, not production-certified"


def render(work: Path, pages_json: Path = None) -> Path:
    out = work / "report.pdf"
    cmd = ["node", str(HERE / "render.mjs"), str(MD), str(work / "report.html"), str(out)]
    if pages_json:
        cmd.append(str(pages_json))
    subprocess.run(cmd, check=True)
    return out


def locate(pdf_path: Path, sections: list) -> dict:
    doc = pymupdf.open(pdf_path)
    found = {}
    start = 2  # cover (0) and contents (1) never hold a section heading

    for section in sections:
        title = html.unescape(section["title"])
        number = section["number"]
        wanted = {f"{number} {title}", title} if number else {title}
        for index in range(start, doc.page_count):
            lines = [" ".join(line.split()) for line in doc[index].get_text().splitlines()]
            joined = [f"{a} {b}" for a, b in zip(lines, lines[1:])]
            hit = any(line in wanted for line in lines) if not number else (
                f"{number} {title}" in lines or f"{number} {title}" in joined
            )
            if hit:
                found[section["id"]] = index + 1
                start = index
                break

    doc.close()
    return found


def stamp(src: Path, dst: Path):
    doc = pymupdf.open(src)
    total = doc.page_count

    for index in range(1, total):
        page = doc[index]
        w, h = page.rect.width, page.rect.height
        left, right = 42, w - 42

        page.draw_line((left, 38), (right, 38), color=TEAL, width=0.8)
        page.insert_text((left, 32), HEADER_LEFT, fontname="helv", fontsize=7, color=TEAL)
        page.insert_text((right - pymupdf.get_text_length(HEADER_RIGHT, "helv", 7), 32), HEADER_RIGHT, fontname="helv", fontsize=7, color=MUTED)

        page.draw_line((left, h - 36), (right, h - 36), color=(0.85, 0.88, 0.91), width=0.6)
        page.insert_text((left, h - 24), FOOTER_LEFT, fontname="helv", fontsize=6.8, color=MUTED)
        label = f"Page {index + 1} of {total}"
        page.insert_text((right - pymupdf.get_text_length(label, "hebo", 7.5), h - 24), label, fontname="hebo", fontsize=7.5, color=TEAL)

    doc.set_metadata({
        "title": "JeevaFlow — Secure Healthcare Document Intelligence & Doctor-Ready Clinical Workflow",
        "author": "Team Geek",
        "subject": "Healthcare Data Security & Clinical Workflow Architecture Report",
        "keywords": "JeevaFlow, healthcare data security, consent, provenance, audit chain",
        "creator": "docs/report/build/build_pdf.py",
        "producer": "Chrome (Playwright) + PyMuPDF",
    })
    doc.save(dst, garbage=3, deflate=True)
    doc.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        first = render(work)
        sections = json.loads((work / "report.sections.json").read_text())
        pages = locate(first, sections)

        missing = [s["id"] for s in sections if s["id"] not in pages]
        if missing:
            sys.exit(f"Could not locate sections: {missing}")

        pages_json = work / "pages.json"
        pages_json.write_text(json.dumps(pages))
        second = render(work, pages_json)

        # The TOC length is fixed, so page positions must not move between passes.
        if locate(second, sections) != pages:
            sys.exit("Section pages changed between passes.")

        stamp(second, PDF)
        (REPORT / "build" / "last_build_pages.json").write_text(json.dumps(pages, indent=1))

    doc = pymupdf.open(PDF)
    print(f"wrote {PDF} ({doc.page_count} pages)")
    doc.close()


if __name__ == "__main__":
    main()
