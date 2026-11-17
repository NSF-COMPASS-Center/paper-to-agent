import shutil

import pymupdf
import pytest

from paperskill.ocr import ocr_note, ocr_substitute


def test_note_names_the_pages():
    assert "7, 12" in ocr_note([7, 12]) and "OCR" in ocr_note([7])


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_substitution_reports_pages_and_leaves_others_untouched(tmp_path):
    src = tmp_path / "in.pdf"
    doc = pymupdf.open()
    for text in ("first page marker", "second page text about rainfall"):
        doc.new_page().insert_text((72, 100), text, fontsize=24)
    doc.save(str(src))
    res = ocr_substitute(src, tmp_path / "out" / "in.pdf", [2, 9])
    assert res["done"] == [2] and 9 in res["failed"]
    out = pymupdf.open(str(tmp_path / "out" / "in.pdf"))
    assert len(out) == 2 and "first page marker" in out[0].get_text()
    assert "rainfall" in out[1].get_text().lower()
