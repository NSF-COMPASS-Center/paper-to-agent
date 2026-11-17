import json
from pathlib import Path

import pymupdf

from paperskill.crops import crop_growth, repair_crops

FX = json.loads((Path(__file__).parent / "fixtures" / "accounting_pages.json").read_text())


def test_paper3_p27_grows_left_to_include_row_labels():
    page = FX["p3_p27"]
    growth, blocked = crop_growth([tuple(w) for w in page["words"]], page["items"])
    assert set(growth) == {"p0027-b000"} and set(growth["p0027-b000"]) == {"left"}
    assert 60 < growth["p0027-b000"]["left"] <= 80 and blocked == []


def test_clean_pages_are_left_alone():
    for tag in ("p18_p3", "p15_p7"):
        page = FX[tag]
        assert crop_growth([tuple(w) for w in page["words"]], page["items"]) == ({}, [])


def _page(tmp_path, label_x):
    sd = tmp_path / "s001"
    (sd / "pages").mkdir(parents=True)
    doc = pymupdf.open()
    pg = doc.new_page(width=300, height=300)
    pg.insert_text((label_x, 100), "ROWLABEL", fontsize=10)
    pg.insert_text((120, 100), "42", fontsize=10)
    doc.save(str(sd / "source.pdf"))
    plan = {"page": 1, "width": 300, "height": 300, "reviewed": True,
            "review_notes": "reviewed carefully, nothing odd on this page",
            "items": [{"id": "p0001-b000", "kind": "table", "bbox": [100.0, 60.0, 250.0, 130.0],
                       "markdown": "|42|", "rows": [["42"]]}]}
    (sd / "pages" / "page-0001.json").write_text(json.dumps(plan))
    return sd


def test_repair_grows_the_flagged_side_once_and_is_idempotent(tmp_path):
    sd = _page(tmp_path, 40)
    first = repair_crops([sd])
    assert any(r["applied"] for r in first)
    plan = json.loads((sd / "pages" / "page-0001.json").read_text())
    assert plan["items"][0]["bbox"][0] < 40
    assert "enlarged" in plan["review_notes"]
    assert repair_crops([sd]) == []


def test_a_word_beyond_the_limit_is_reported_not_chased(tmp_path):
    sd = _page(tmp_path, 5)       # 95 pt left of the crop
    res = repair_crops([sd])
    assert not any(r["applied"] for r in res)
    plan = json.loads((sd / "pages" / "page-0001.json").read_text())
    assert plan["items"][0]["bbox"][0] == 100.0
