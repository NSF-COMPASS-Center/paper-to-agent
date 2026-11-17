"""Page editor invariants. No API key needed."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paperskill.editor import EditError, PageEditor  # noqa: E402


def make_page(tmp_path, items=None):
    items = items or [
        {"id": "p1-t1", "kind": "text", "markdown": "Left column opens here.", "bbox": [50, 50, 280, 200]},
        {"id": "p1-t2", "kind": "text", "markdown": "Right column continues the thought.", "bbox": [300, 50, 540, 200]},
        {"id": "p1-f1", "kind": "figure", "markdown": "", "label": "Figure 1",
         "asset_name": "figure-1", "bbox": [50, 220, 540, 400]},
        {"id": "p1-c1", "kind": "caption", "markdown": "Fig. 1. A caption.", "bbox": [50, 405, 540, 420]},
    ]
    p = tmp_path / "page-0001.json"
    p.write_text(json.dumps({"page": 1, "width": 595, "height": 842, "mode": "native",
                             "reviewed": False, "review_notes": "", "items": items}))
    return p


def test_reorder_requires_exact_permutation(tmp_path):
    e = PageEditor(make_page(tmp_path))
    e.reorder(["p1-t1", "p1-f1", "p1-c1", "p1-t2"])
    assert [i["id"] for i in e.items] == ["p1-t1", "p1-f1", "p1-c1", "p1-t2"]
    with pytest.raises(EditError, match="every current item id"):
        e.reorder(["p1-t1", "p1-t2"])
    with pytest.raises(EditError, match="unknown"):
        e.reorder(["p1-t1", "p1-t2", "p1-f1", "p1-c1", "p1-nope"])


def test_markdown_cannot_be_emptied_and_figures_reject_markdown(tmp_path):
    e = PageEditor(make_page(tmp_path))
    with pytest.raises(EditError, match="cannot be emptied"):
        e.set_markdown("p1-t1", "   ")
    with pytest.raises(EditError, match="ignored by"):
        e.set_markdown("p1-f1", "anything")


def test_split_then_both_halves_survive_validation(tmp_path):
    e = PageEditor(make_page(tmp_path))
    a, b = e.split("p1-t2", "continues the thought.")
    assert [a, b] == ["p1-t2-a", "p1-t2-b"]
    assert e.get(a)["markdown"] == "Right column"
    assert e.validate() == []


def test_split_requires_text_present(tmp_path):
    e = PageEditor(make_page(tmp_path))
    with pytest.raises(EditError, match="not found"):
        e.split("p1-t1", "absent phrase")


def test_merge_keeps_first_and_refuses_mixed_kinds(tmp_path):
    e = PageEditor(make_page(tmp_path))
    kept = e.merge(["p1-t1", "p1-t2"])
    assert kept == "p1-t1"
    assert "Left column opens here. Right column" in e.get("p1-t1")["markdown"]
    assert e.validate() == []
    e2 = PageEditor(make_page(tmp_path))
    with pytest.raises(EditError, match="different kinds"):
        e2.merge(["p1-t1", "p1-f1"])


def test_deleting_an_item_outright_fails_validation(tmp_path):
    e = PageEditor(make_page(tmp_path))
    del e.items[0]
    assert any("disappeared" in p for p in e.validate())
    with pytest.raises(EditError, match="refusing to save"):
        e.save()


def test_omit_requires_a_real_reason(tmp_path):
    e = PageEditor(make_page(tmp_path))
    with pytest.raises(EditError, match="specific reason"):
        e.set_kind("p1-t1", "omit", "junk")
    e.set_kind("p1-t1", "omit", "running header repeated on every page, checked on the render")
    assert e.validate() == []


def test_join_previous_needs_preceding_prose_and_rejects_code(tmp_path):
    e = PageEditor(make_page(tmp_path))
    e.set_join_previous("p1-t2", "space")
    assert e.get("p1-t2")["join_previous"] == "space"
    with pytest.raises(EditError, match="first item"):
        e.set_join_previous("p1-t1", "space")
    with pytest.raises(EditError, match="needs preceding prose"):
        e.set_join_previous("p1-c1", "space")        # preceded by the figure
    e.set_kind("p1-t2", "code")
    with pytest.raises(EditError, match="fenced blocks"):
        e.set_join_previous("p1-t2", "space")


def test_expand_bbox_is_capped_clipped_and_figure_only(tmp_path):
    e = PageEditor(make_page(tmp_path))
    bb = e.expand_bbox("p1-f1", "left", 20, cap=24)
    assert bb[0] == 30
    with pytest.raises(EditError, match="capped at"):
        e.expand_bbox("p1-f1", "left", 99, cap=24)
    with pytest.raises(EditError, match="only figure"):
        e.expand_bbox("p1-t1", "left", 5, cap=24)
    e.expand_bbox("p1-f1", "right", 24, cap=24)
    assert e.get("p1-f1")["bbox"][2] == 564          # clipped below width 595
    with pytest.raises(EditError, match="collapse"):
        e.expand_bbox("p1-f1", "top", -200, cap=240)


def test_asset_name_must_be_slug(tmp_path):
    e = PageEditor(make_page(tmp_path))
    with pytest.raises(EditError, match="lowercase, hyphenated"):
        e.set_asset("p1-f1", asset_name="Figure 1.jpg")
    e.set_asset("p1-f1", label="Figure 1", asset_name="figure-1")


def test_table_rows_must_be_rectangular_strings(tmp_path):
    p = make_page(tmp_path, [{"id": "p1-tb1", "kind": "table", "markdown": "",
                              "label": "Table 1", "asset_name": "table-1",
                              "bbox": [50, 50, 540, 300]}])
    e = PageEditor(p)
    with pytest.raises(EditError, match="same number of columns"):
        e.set_rows("p1-tb1", [["a", "b"], ["c"]])
    with pytest.raises(EditError, match="must be strings"):
        e.set_rows("p1-tb1", [["a", "b"], ["c", 1]])
    e.set_rows("p1-tb1", [["Sample", "P value"], ["001", "1.2e-05"]])
    with pytest.raises(EditError, match="transcribe it first"):
        e.set_rows("p1-tb1", None)                   # a bare fallback is refused
    e.set_rows("p1-tb1", None, reason="Rightmost three columns are cut off in the source scan.")
    assert e.get("p1-tb1")["rows"] is None


def test_generic_review_notes_are_rejected(tmp_path):
    e = PageEditor(make_page(tmp_path))
    with pytest.raises(EditError, match="specifically"):
        e.mark_reviewed("looks fine")
    e.mark_reviewed("Confirmed two-column reading order; caption follows Figure 1; "
                    "repaired a corrupted '>' in the first paragraph.")
    e.save()
    assert json.loads(e.path.read_text())["reviewed"] is True


def test_save_round_trips_and_records_ops(tmp_path):
    e = PageEditor(make_page(tmp_path))
    e.reorder(["p1-t1", "p1-t2", "p1-f1", "p1-c1"])
    e.set_markdown("p1-t1", "Left column opens here, with a ≥ symbol restored.")
    e.mark_reviewed("Checked reading order against the render and restored one comparison "
                    "symbol that the font encoding had mangled.")
    e.save()
    data = json.loads(e.path.read_text())
    assert "≥" in data["items"][0]["markdown"]
    assert "Changes:" not in data["review_notes"]     # tools append that, not the editor
    assert len(e.ops) == 3


# --- phantom repairs ---------------------------------------------------------
# A model reported four text repairs on one page; three were no-op writes of
# identical text, and the ops log recorded them as "166->166 chars", which is
# also what a real length-preserving repair looks like. Both halves are fixed:
# a no-op is refused, and a real change records what it changed.

def test_no_op_markdown_write_is_refused(tmp_path):
    e = PageEditor(make_page(tmp_path))
    same = e.get("p1-t1")["markdown"]
    with pytest.raises(EditError, match="already contains exactly that text"):
        e.set_markdown("p1-t1", same)
    assert e.ops == []


def test_length_preserving_repair_is_recorded_with_its_diff(tmp_path):
    p = make_page(tmp_path, [{"id": "p1-t1", "kind": "text",
                              "markdown": "Editor: D. Barcelo", "bbox": [50, 50, 280, 80]}])
    e = PageEditor(p)
    e.set_markdown("p1-t1", "Editor: D. Barceló")
    op = e.ops[0]
    assert "18->18 chars" in op              # the old signal, still there
    assert "Barcelo" in op or "'o'->" in op  # and now what actually changed
    assert "no change" not in op


def test_describe_change_reports_insertions_and_substitutions():
    from paperskill.editor import describe_change
    assert describe_change("abc", "abc") == "no change"
    d = describe_change("Accepted 12 April 2016",
                        "Accepted 12 April 2016\nAvailable online 29 April 2016")
    assert "Available online" in d
    d2 = describe_change("the >value", "the N value")
    assert "@" in d2


def test_describe_change_caps_the_number_of_spans():
    from paperskill.editor import describe_change
    d = describe_change("a1b2c3d4e5", "aXbYcZdWeV")
    assert d.count("@") <= 3
    assert d.endswith("...")
