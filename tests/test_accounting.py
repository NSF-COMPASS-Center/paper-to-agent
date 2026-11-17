import json
from pathlib import Path

from paperskill.accounting import account_page, garbled_items

FX = json.loads((Path(__file__).parent / "fixtures" / "accounting_pages.json").read_text())


def lost(tag):
    page = FX[tag]
    return account_page([tuple(w) for w in page["words"]], page["items"])["unaccounted"]


def test_paper3_p27_finds_the_row_below_the_table_crop():
    assert "159" in [u["word"] for u in lost("p3_p27")]


def test_paper15_p7_finds_text_removed_by_omit():
    words = {u["word"].strip(".,:;()").lower() for u in lost("p15_p7")}
    assert {"rainfall", "hepatitis"} <= words or {"rainfall", "incidence"} <= words


def test_clean_page_has_no_losses():
    assert lost("p18_p3") == []


def _w(text, x=0.0, y=0.0):
    return (x, y, x + 20, y + 10, text)


def test_furniture_omit_is_exempt_but_content_omit_is_not():
    items = [{"id": "a", "kind": "text", "bbox": [0, 100, 50, 120], "markdown": "kept words here"},
             {"id": "b", "kind": "omit", "bbox": [0, 0, 50, 20], "source_class": "page-header",
              "reason": "running header", "markdown": "Journal Header"},
             {"id": "c", "kind": "omit", "bbox": [0, 200, 50, 220], "source_class": "text",
              "reason": "Duplicate fragment", "markdown": "stray sentence"}]
    out = account_page([_w("Journal", 1, 1), _w("stray", 1, 201)], items)["unaccounted"]
    assert [(u["word"], u["cause"]) for u in out] == [("stray", "omitted")]


def test_ligatures_ranges_and_superscripts_match():
    items = [{"id": "a", "kind": "text", "bbox": [0, 0, 5, 5],
              "markdown": "quantified 1-3 and 10<sup>6</sup> and p < 0.05 later words"}]
    words = [_w("quantiﬁed"), _w("1–3"), _w("106"), _w("later")]
    assert account_page(words, items)["unaccounted"] == []


def test_short_number_must_match_as_a_whole_token():
    items = [{"id": "a", "kind": "text", "bbox": [0, 0, 5, 5], "markdown": "value 1159 only"}]
    assert [u["word"] for u in account_page([_w("159")], items)["unaccounted"]] == ["159"]


def test_word_inside_a_crop_is_covered():
    items = [{"id": "t", "kind": "table", "bbox": [0, 0, 100, 100], "markdown": ""}]
    assert account_page([_w("anything", 10, 10)], items)["unaccounted"] == []


def test_garbled_items_detects_word_per_line_text():
    md = "<br>".join(["word"] * 40)
    assert garbled_items([{"id": "x", "markdown": md}, {"id": "y", "markdown": "normal prose"}]) == ["x"]


def test_margin_line_numbers_are_not_reported_but_a_lone_number_is():
    items = [{"id": "a", "kind": "text", "bbox": [100, 0, 500, 900], "markdown": "body words only"}]
    column = [_w(str(n), 20, 10 * n) for n in range(50, 56)]
    assert account_page(column, items)["unaccounted"] == []
    assert [u["word"] for u in account_page([_w("159", 20, 5)], items)["unaccounted"]] == ["159"]
