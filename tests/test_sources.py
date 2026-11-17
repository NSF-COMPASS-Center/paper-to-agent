import json

from paperskill.sources import copy_source, original_pdf


def test_copy_goes_beside_the_package_not_inside_it(tmp_path):
    pdf = tmp_path / "orig.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    pkg = tmp_path / "dist" / "paper-9"
    pkg.mkdir(parents=True)
    dest = copy_source(pkg, pdf)
    assert dest == tmp_path / "dist" / "_sources" / "paper-9.pdf" and dest.read_bytes() == pdf.read_bytes()
    assert list(pkg.iterdir()) == []          # the package's file set is untouched


def test_original_prefers_the_ocr_source_and_falls_back_to_inventory(tmp_path):
    real = tmp_path / "real.pdf"
    real.write_bytes(b"x")
    work = tmp_path / "w"
    work.mkdir()
    (work / "inventory.json").write_text(json.dumps(
        {"sources": [{"kind": "pdf", "original_path": str(tmp_path / "ocr-copy.pdf")}]}))
    (tmp_path / "ocr-copy.pdf").write_bytes(b"y")
    assert original_pdf(work) == tmp_path / "ocr-copy.pdf"
    assert original_pdf(work, str(real)) == real


def test_copy_source_onto_itself_is_a_noop(tmp_path):
    pkg = tmp_path / "dist" / "p"
    pkg.mkdir(parents=True)
    src = tmp_path / "dist" / "_sources"
    src.mkdir()
    pdf = src / "p.pdf"
    pdf.write_bytes(b"%PDF")
    assert copy_source(pkg, pdf) == pdf and pdf.read_bytes() == b"%PDF"
