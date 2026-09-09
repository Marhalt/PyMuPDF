import argparse
import math
import re
from collections import defaultdict
from pathlib import Path

import pymupdf  # PyMuPDF (formerly imported as `fitz`)

# --- tuning knobs ---------------------------------------------------------------
TOP_ZONE = 0.12  # a line whose vertical center sits in the top 12% is a header candidate
BOT_ZONE = 0.88  # ... bottom 12% is a footer candidate
MAX_CANDIDATE_LEN = 80  # running headers/footers are short; longer lines are body text
GLOBAL_FRAC = 0.30  # furniture if the same (zone, text) recurs on >= 30% of pages
RUN_MIN_PAGES = 4  # section-header run: min pages the text must appear on
RUN_MIN_DENSITY = 0.55  # ... and it must fill >= 55% of the page span it covers
NUMERIC_FRAC = 0.60  # a candidate line that is >= 60% digits is treated as a page number
NUMERIC_MAXLEN = 10  # ... but only if it is short (avoids catching data-heavy body lines)


def select_pdf():
    # Fallback GUI picker when no path is passed on the command line
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    file_path = filedialog.askopenfilename(
        title="Select PDF Book to Clean",
        filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
    )
    return file_path


# --- text helpers -------------------------------------------------------------


def normalize(s: str) -> str:
    """Collapse a line to a comparison key: digits -> '#', drop punctuation/case."""
    s = s.lower()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^\w#]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def is_page_number(s: str) -> bool:
    """A short, mostly-numeric line ('123', '- 47 -', 'xii' stays out)."""
    chars = re.sub(r"\s", "", s)
    if not chars or len(chars) > NUMERIC_MAXLEN:
        return False
    digits = sum(c.isdigit() for c in chars)
    return digits / len(chars) >= NUMERIC_FRAC


def zone_of(y0f: float, y1f: float):
    yc = (y0f + y1f) / 2
    if yc <= TOP_ZONE:
        return "top"
    if yc >= BOT_ZONE:
        return "bottom"
    return None


# --- detection ----------------------------------------------------------------


def extract_lines(doc):
    """Per page: a list of (text, y0_fraction, y1_fraction), in reading order.

    Coordinates are page-space; rotated pages are handled by PyMuPDF's rect/bbox
    already being in the rotated frame, so the fractions stay meaningful.
    """
    pages = []
    for page in doc:
        height = page.rect.height or 1.0
        data = page.get_text("dict", sort=True)
        lines = []
        for block in data["blocks"]:
            if block.get("type", 0) != 0:  # skip image blocks
                continue
            for ln in block["lines"]:
                text = "".join(span["text"] for span in ln["spans"]).strip()
                if not text:
                    continue
                _, y0, _, y1 = ln["bbox"]
                lines.append((text, y0 / height, y1 / height))
        pages.append(lines)
    return pages


def build_furniture_model(pages):
    """Return {(zone, normalized_text): (reason, page_count)} for running headers/footers."""
    n_pages = len(pages)
    occurrences = defaultdict(set)  # (zone, norm) -> set of page indices

    for i, lines in enumerate(pages):
        for text, y0f, y1f in lines:
            zone = zone_of(y0f, y1f)
            if not zone or len(text) > MAX_CANDIDATE_LEN:
                continue
            norm = normalize(text)
            if norm:
                occurrences[(zone, norm)].add(i)

    global_min = max(RUN_MIN_PAGES, math.ceil(GLOBAL_FRAC * n_pages))
    furniture = {}
    for key, page_set in occurrences.items():
        count = len(page_set)
        if count >= global_min:
            furniture[key] = ("recurring", count)
            continue
        # A running header may live on only one side (verso/recto), so also test
        # each parity on its own timeline before giving up.
        for pages_subset in (page_set, _parity(page_set, 0), _parity(page_set, 1)):
            if len(pages_subset) < RUN_MIN_PAGES:
                continue
            lo, hi = min(pages_subset), max(pages_subset)
            slots = (hi - lo) // 2 + 1 if pages_subset is not page_set else hi - lo + 1
            if slots >= 3 and len(pages_subset) / slots >= RUN_MIN_DENSITY:
                furniture[key] = ("section run", count)
                break
    return furniture


def _parity(page_set, which):
    return {p for p in page_set if p % 2 == which}


def clean_page(lines, furniture, stats):
    kept = []
    for text, y0f, y1f in lines:
        zone = zone_of(y0f, y1f)
        if zone and len(text) <= MAX_CANDIDATE_LEN:
            if is_page_number(text):
                stats["page numbers"] += 1
                continue
            key = (zone, normalize(text))
            if key in furniture:
                stats[key] += 1
                continue
        kept.append(text)
    return kept


# --- pipelines ---------------------------------------------------------------


def clean_pdf_simple(pdf_path: Path, output_dir: Path):
    """Original approach: blindly drop the top/bottom 10% of every page."""
    doc = pymupdf.open(pdf_path)
    print(f"Processing {len(doc)} pages (simple crop)...")
    clean_pages = []
    for page in doc:
        rect = page.rect
        crop_box = pymupdf.Rect(
            rect.x0,
            rect.y0 + rect.height * 0.10,
            rect.x1,
            rect.y1 - rect.height * 0.10,
        )
        page_text = page.get_text("text", clip=crop_box).strip()
        if page_text:
            clean_pages.append(page_text)
    doc.close()
    _write(output_dir, pdf_path, "\n\n".join(clean_pages))


def clean_pdf_smart(pdf_path: Path, output_dir: Path):
    """Detect running headers/footers/page numbers by repetition + position."""
    doc = pymupdf.open(pdf_path)
    print(f"Processing {len(doc)} pages...")
    pages = extract_lines(doc)
    doc.close()

    if len(pages) < 5:
        print("  (few pages: only stripping obvious page numbers, no repetition model)")

    furniture = build_furniture_model(pages)
    stats = defaultdict(int)
    out_pages = []
    for lines in pages:
        kept = clean_page(lines, furniture, stats)
        if kept:
            out_pages.append("\n".join(kept))

    _report(furniture, stats, len(pages))
    _write(output_dir, pdf_path, "\n\n".join(out_pages))


def _report(furniture, stats, n_pages):
    if not stats:
        print("  No headers/footers detected.")
        return
    print("  Detected running furniture:")
    for key, (reason, count) in sorted(
        furniture.items(), key=lambda kv: -kv[1][1]
    ):
        zone, norm = key
        removed = stats.get(key, 0)
        shown = norm if norm else "(blank)"
        print(f"    [{zone:>6}] {shown!r:<45} {reason}, {count}/{n_pages} pages, {removed} lines cut")
    if stats.get("page numbers"):
        print(f"    [ both ] numeric page numbers{'':<24} {stats['page numbers']} lines cut")
    total = sum(stats.values())
    print(f"  Removed {total} lines total.")


def _write(output_dir: Path, pdf_path: Path, text: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{pdf_path.stem}_cleaned.txt"
    out_path.write_text(text, encoding="utf-8")
    print(f"  Saved: {out_path}")


# --- entry point -----------------------------------------------------------


def run(target: str = None, simple: bool = False):
    if not target:
        target = select_pdf()
    if not target:
        print("No file selected. Operation canceled.")
        return

    path = Path(target).expanduser()
    if not path.exists():
        print(f"Path does not exist: {path}")
        return

    clean = clean_pdf_simple if simple else clean_pdf_smart

    if path.is_file():
        if path.suffix.lower() != ".pdf":
            print(f"Not a PDF file: {path}")
            return
        print(f"Opening: {path.name}")
        clean(path, path.parent / "clean")
        return

    pdf_files = sorted(path.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDF files found in: {path}")
        return
    output_dir = path / "clean"
    print(f"Found {len(pdf_files)} PDF file(s) in {path}")
    for pdf_file in pdf_files:
        print(f"\nOpening: {pdf_file.name}")
        clean(pdf_file, output_dir)
    print(f"\nDone. Cleaned files are in:\n{output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract clean body text from PDF(s), dropping running headers/footers/page numbers."
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="a PDF file or a directory of PDFs (omit to open a file picker)",
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        help="use the old fixed 10%% top/bottom crop instead of detection",
    )
    args = parser.parse_args()
    run(args.path, simple=args.simple)
