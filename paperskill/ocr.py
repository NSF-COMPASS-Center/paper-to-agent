"""Per-page OCR for pages whose native text layer is unusable.

The engine's `--ocr always` is whole-document, and `auto` only fires on pages with no
text layer. Paper 15 p7 has a text layer, but it is one word per line in a table, so
the extractor emitted `word<br> word<br> ...` and the reviewer could not recover the
prose (it omitted fragments as "duplicates" and lost "rainfall", "hepatitis"...).
Tesseract reads the page image correctly (about 1385 words, in reading order).

The vendored engine stays unmodified: the chosen pages are replaced by their OCR'd
versions in a copy of the input PDF, and the copy is what gets prepared. The original
is untouched and its hash is recorded by the caller.
"""
from __future__ import annotations

from pathlib import Path

OCR_DPI = 300


def ocr_substitute(src: Path, dst: Path, pages: list[int], language: str = "eng") -> dict:
    """Copy `src` to `dst` with the 1-based `pages` replaced by OCR output.

    Returns {"done": [...], "failed": {page: reason}}. A page that cannot be OCR'd is
    copied unchanged, so a failure never makes the document worse than the original.
    """
    import pymupdf
    done: list[int] = []
    failed: dict[int, str] = {}
    source = pymupdf.open(str(src))
    out = pymupdf.open()
    wanted = set(pages)
    for i in range(len(source)):
        page_no = i + 1
        if page_no in wanted:
            try:
                p = source[i]
                p.remove_rotation()
                raw = p.get_pixmap(dpi=OCR_DPI, alpha=False).pdfocr_tobytes(language=language)
                ocr = pymupdf.open(stream=raw, filetype="pdf")
                if not ocr[0].get_text().strip():
                    raise ValueError("OCR returned no text")
                out.insert_pdf(ocr)
                done.append(page_no)
                continue
            except Exception as exc:
                failed[page_no] = f"{type(exc).__name__}: {exc}"
        out.insert_pdf(source, from_page=i, to_page=i)
    for page_no in sorted(wanted - set(range(1, len(source) + 1))):
        failed[page_no] = "page is outside the document"
    out.set_metadata(source.metadata)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(dst))
    return {"done": done, "failed": failed}


def ocr_note(pages: list[int]) -> str:
    return ("Text on PDF page(s) " + ", ".join(map(str, pages)) + " was recovered by OCR "
            "because the PDF's own text layer was unusable; check numbers and symbols "
            "on those pages against the source.")
