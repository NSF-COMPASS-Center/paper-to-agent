import json

import pymupdf

from paperskill.tablefix import (column_count, join_lines, looks_like_prose_table,
                                 repair_prose_tables)


def box(md, cls="table", kind="table"):
    return {"id": "p0001-b000", "kind": kind, "source_class": cls, "markdown": md}


# strings copied from the layout model's output on paper 15
WORD_PER_CELL = ("|Evaluation<br> of<br> the<br> physical<br> and<br> chemical<br> "
                 "characteristics<br> of<br> water<br> on|\n|---|")
BODY_PROSE_4COL = ("|hown<br>|in<br> the<br> results,<br> rotaviruses<br> exist<br> in<br> "
                   "the<br> raw<br> water<br> and<br> were<br> found|x<br> y<br>|z|\n|---|---|---|---|")
REAL_TABLE = "|Group|Subgroup|Est. LRV|p-value|\n|---|---|---|---|\n|A|B|1.2|0.03|"
PAGE_NUMBER = "|7|\n|---|"


def test_word_per_cell_one_column_box_is_prose():
    assert looks_like_prose_table(box(WORD_PER_CELL))


def test_one_column_page_number_box_is_not_a_table():
    assert looks_like_prose_table(box(PAGE_NUMBER))


def test_wide_box_needs_an_overwhelming_signature():
    assert looks_like_prose_table(box(BODY_PROSE_4COL))
    assert not looks_like_prose_table(box(REAL_TABLE))


def test_only_table_class_boxes_are_considered():
    assert not looks_like_prose_table(box(WORD_PER_CELL, cls="text", kind="text"))


def test_column_count():
    assert column_count(REAL_TABLE) == 4 and column_count(PAGE_NUMBER) == 1


def test_hyphenated_wrap_is_joined_and_plain_lines_get_a_space():
    assert join_lines(["efficien-", "cies matter", "here"]) == "efficiencies matter here"
    assert join_lines(["Pre-", "Treatment stays"]) == "Pre- Treatment stays"


def _pdf(tmp_path, reviewed):
    sd = tmp_path / "s001"
    (sd / "pages").mkdir(parents=True)
    doc = pymupdf.open()
    pg = doc.new_page(width=300, height=300)
    pg.insert_textbox(pymupdf.Rect(40, 40, 260, 120),
                      "Rotavirus removal was measured in the raw water samples.", fontsize=11)
    doc.save(str(sd / "source.pdf"))
    plan = {"page": 1, "reviewed": reviewed, "warnings": [],
            "items": [{"id": "p0001-b000", "kind": "table", "source_class": "table",
                       "bbox": [30.0, 30.0, 270.0, 130.0], "markdown": WORD_PER_CELL,
                       "rows": None, "label": "Table on PDF page 1 (1)",
                       "asset_name": "table-p0001-000"}]}
    (sd / "pages" / "page-0001.json").write_text(json.dumps(plan))
    return sd


def test_repair_rebuilds_prose_from_the_pdf_and_is_idempotent(tmp_path):
    sd = _pdf(tmp_path, reviewed=False)
    res = repair_prose_tables([sd])
    assert len(res) == 1
    item = json.loads((sd / "pages" / "page-0001.json").read_text())["items"][0]
    assert item["kind"] == "text" and "Rotavirus removal" in item["markdown"]
    assert "asset_name" not in item and item["extractor_class"] == "table"
    assert repair_prose_tables([sd]) == []


def test_reviewed_pages_are_never_touched(tmp_path):
    sd = _pdf(tmp_path, reviewed=True)
    assert repair_prose_tables([sd]) == []
    item = json.loads((sd / "pages" / "page-0001.json").read_text())["items"][0]
    assert item["kind"] == "table"
