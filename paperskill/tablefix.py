"""Paragraphs that the layout model labelled as tables.

The engine runs pymupdf4llm with its layout model. On some PDFs (paper 15, an Elsevier
Distiller file whose text layer is 3.3 characters per span) the model labels most
regions `table`, and each word becomes a cell: `|Evaluation<br> of<br> the<br> ...|`.
With the layout model off the same page extracts as ordinary prose, so the words are
fine; the classification is wrong. PyMuPDF's own table finder finds no table on those
pages.

Rebuild such regions from the PDF's text blocks. Runs once, right after extraction and
before any model sees the page, so the reviewer gets prose instead of a haystack of
`<br>` cells. Reviewed pages are never touched. A box is rewritten only when ALL hold:
it is a one-column box, or a two-column one with a word-per-cell signature, or a wider
one with an overwhelming signature (about one word per `<br>`), and PyMuPDF finds no
ruled table inside it. Tables with numbers in many columns are left alone.

Pure and SDK-free; pymupdf is the only dependency.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# A markdown table of one column is a text box the model boxed, not a table. Two
# columns need a clear word-per-cell signature; more columns need an overwhelming one
# (paper 15 p5: 4 "columns" of body prose, 17 breaks, 1.3 words per segment).
TWO_COL_BREAKS, TWO_COL_WORDS = 6, 3.0
MANY_COL_BREAKS, MANY_COL_WORDS = 10, 1.6

_HYPHEN_BREAK = re.compile(r"[A-Za-z]-$")


def column_count(markdown: str) -> int:
    first = next((ln for ln in markdown.splitlines() if ln.strip().startswith("|")), "")
    return max(first.strip().strip("|").count("|") + 1, 1) if first else 0


def looks_like_prose_table(item: dict) -> bool:
    if item.get("source_class") != "table" or item.get("kind") not in {"table", "text"}:
        return False
    md = item.get("markdown") or ""
    cols = column_count(md)
    if cols == 0:
        return False
    if cols == 1:
        return True
    breaks = md.count("<br>")
    wps = len(md.replace("<br>", " ").replace("|", " ").split()) / (breaks + 1)
    if cols == 2:
        return breaks >= TWO_COL_BREAKS and wps <= TWO_COL_WORDS
    return breaks >= MANY_COL_BREAKS and wps <= MANY_COL_WORDS


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines. A line ending in a letter and a hyphen is joined to a
    lower-case continuation without the hyphen ("coagula-" + "tion"); a real compound
    broken at a hyphen ("non-" + "enveloped") loses its hyphen, which the verifier
    does not see and which reads as one word."""
    out = ""
    for ln in (l.strip() for l in lines):
        if not ln:
            continue
        if out and _HYPHEN_BREAK.search(out) and ln[:1].islower():
            out = out[:-1] + ln
        else:
            out = f"{out} {ln}" if out else ln
    return out


LINE_GAP_PT = 4.0       # lines of one paragraph sit this close (or overlap)
INDENT_PT = 12.0       # left edges of one paragraph agree within this


def merge_lines(blocks: list[tuple[list[float], str]]) -> list[tuple[list[float], str]]:
    """This kind of PDF makes every printed line its own block. Consecutive blocks that
    stack tightly under the same left edge are one paragraph, so join them; otherwise a
    sentence is shattered into line-sized items ("...clarification" / "and filtration")."""
    out: list[tuple[list[float], str]] = []
    for bb, text in blocks:
        if out:
            pb, ptext = out[-1]
            stacked = 0 <= bb[1] - pb[1] and bb[1] - pb[3] <= LINE_GAP_PT
            same_edge = abs(bb[0] - pb[0]) <= INDENT_PT
            if stacked and same_edge:
                out[-1] = ([min(pb[0], bb[0]), pb[1], max(pb[2], bb[2]), max(pb[3], bb[3])],
                           join_lines([ptext, text]))
                continue
        out.append((bb, text))
    return out


def text_blocks(page, bbox: list[float]) -> list[tuple[list[float], str]]:
    """(bbox, text) for each paragraph in the box, in the PDF's own block order."""
    import pymupdf
    clip = pymupdf.Rect(*bbox)
    out = []
    for b in page.get_text("blocks", clip=clip):
        if b[6] != 0:
            continue
        text = join_lines(b[4].splitlines())
        if text:
            out.append(([round(v, 1) for v in b[:4]], text))
    return merge_lines(out)


def has_ruled_table(page, bbox: list[float]) -> bool:
    import pymupdf
    try:
        return any(t.row_count >= 2 and t.col_count >= 2
                   for t in page.find_tables(clip=pymupdf.Rect(*bbox)).tables)
    except Exception:
        return False


def repair_prose_tables(plan_dirs: list[Path]) -> list[dict]:
    """Rewrite word-per-cell prose regions on unreviewed pages. Idempotent."""
    import pymupdf
    results: list[dict] = []
    for sd in plan_dirs:
        pdf = sd / "source.pdf"
        if not pdf.exists():
            continue
        doc = pymupdf.open(str(pdf))
        for pp in sorted((sd / "pages").glob("page-*.json")):
            plan = json.loads(pp.read_text())
            if plan.get("reviewed"):
                continue
            page = doc[int(plan["page"]) - 1]
            new_items, changed = [], 0
            for it in plan.get("items", []):
                if not looks_like_prose_table(it) or has_ruled_table(page, it["bbox"]):
                    new_items.append(it)
                    continue
                blocks = text_blocks(page, it["bbox"])
                if not blocks:
                    new_items.append(it)
                    continue
                for n, (bb, text) in enumerate(blocks):
                    ident = it["id"] if n == 0 else f"{it['id']}-s{n}"
                    new_items.append({"id": ident, "kind": "text", "bbox": bb,
                                      "markdown": text, "source_class": "text",
                                      "extractor_class": "table"})
                plan["warnings"] = [w for w in plan.get("warnings", [])
                                    if it["id"] not in w]
                changed += 1
                results.append({"source_id": sd.name, "page": plan["page"], "applied": True,
                                "detail": f"{it['id']}: word-per-cell table -> {len(blocks)} "
                                          "text block(s) from the PDF"})
            if changed:
                plan["items"] = new_items
                plan.setdefault("warnings", []).append(
                    f"{changed} region(s) the layout model called tables were rebuilt as "
                    "prose from the PDF text blocks; check reading order.")
                pp.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
        doc.close()
    return results
