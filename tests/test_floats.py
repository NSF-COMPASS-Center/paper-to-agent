"""Mid-sentence figure/table repair. Fixtures are real page plans from runs/sk18-v2
(the cheaper-model conversion). No API key needed."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paperskill.bundle import (find_float_clusters, hoist_floats,  # noqa: E402
                               plan_float_hoist)
from paperskill.editor import EditError, PageEditor  # noqa: E402

PAGES = json.loads((Path(__file__).parent / "fixtures" / "sk18_float_pages.json").read_text())


def items(p):
    return json.loads(json.dumps(PAGES[str(p)]["items"]))


def plans(p):
    it = items(p)
    return [([it[i]["id"][-4:] for i in cl], plan_float_hoist(it, cl))
            for cl in find_float_clusters(it)]


def test_table_cluster_includes_label_title_and_table():
    it = items(3)
    assert [[it[i]["id"][-4:] for i in c] for c in find_float_clusters(it)] == [
        ["b015", "b016", "b017"]]


@pytest.mark.parametrize("page,cluster,expect", [
    (3, ["b015", "b016", "b017"], {"before": "p0003-b014"}),   # Table 1 after "...and the residual"
    (5, ["b011", "b012"], {"before": "p0005-b010"}),           # Fig 3 after "The relationships"
    (6, ["b002", "b003"], {"after": "p0006-b005"}),            # Fig 4 ahead of a continuation
    (6, ["b007", "b008"], {"before": "p0006-b006"}),
    (8, ["b006", "b007"], {"before": "p0008-b005"}),           # Fig 8 after "that is,"
    (10, ["b002", "b006"], {"after": "p0010-b008"}),           # Fig 10: continuation, no join flag
])
def test_mid_sentence_floats_get_a_clean_break(page, cluster, expect):
    assert (cluster, expect) in plans(page)


@pytest.mark.parametrize("page", [1, 4, 9])
def test_floats_at_clean_breaks_are_left_alone(page):
    # p1: the graphical abstract follows a spaced-out label, not a paragraph
    assert all(plan is None for _, plan in plans(page))


def test_refuses_when_anchor_is_itself_a_continuation():
    it = items(3)
    for x in it:
        if x["id"] == "p0003-b014":
            x["join_previous"] = "space"
    cl = find_float_clusters(it)[0]
    assert "refuse" in plan_float_hoist(it, cl)


def test_hoist_moves_group_and_keeps_every_item(tmp_path):
    p = tmp_path / "page-0003.json"
    p.write_text(json.dumps(PAGES["3"]))
    e = PageEditor(p)
    before_ids = sorted(i["id"] for i in e.items)
    e.hoist(["p0003-b015", "p0003-b016", "p0003-b017"], before="p0003-b014")
    ids = [i["id"] for i in e.items]
    assert ids.index("p0003-b017") + 1 == ids.index("p0003-b014")
    assert sorted(ids) == before_ids and e.validate() == []
    assert e.ops[-1].startswith("hoist_float(")
    with pytest.raises(EditError):
        e.hoist(["p0003-b015"], before="p0003-b015")


class FakeBundle:
    def __init__(self, root):
        self.root = root
        self.plan_dirs = [root]

    def pages(self, sid):
        return sorted(self.root.glob("page-*.json"))


def test_hoist_floats_is_idempotent_and_discloses(tmp_path):
    for k, v in PAGES.items():
        (tmp_path / f"page-{int(k):04d}.json").write_text(json.dumps(v))
    b = FakeBundle(tmp_path)
    dry = hoist_floats(b, apply=False)
    assert dry and not any(r["applied"] for r in dry)
    first = hoist_floats(b)
    assert sum(r["applied"] for r in first) == 8
    assert hoist_floats(b) == []                       # second run changes nothing
    page3 = json.loads((tmp_path / "page-0003.json").read_text())
    assert "moved to the nearest paragraph break" in page3["review_notes"]
    assert [i["id"][-4:] for i in page3["items"]][-4:-1] == ["b015", "b016", "b017"]


def _page(tmp_path, its):
    p = tmp_path / "page-0001.json"
    p.write_text(json.dumps({"page": 1, "width": 595, "height": 842, "reviewed": True,
                             "review_notes": "fixture, checked against the render.",
                             "items": its}))
    return FakeBundle(tmp_path), p


def _t(i, md, **kw):
    return {"id": i, "kind": "text", "markdown": md, "bbox": [0, 0, 100, 40], **kw}


LONG = "This paragraph runs on without reaching an ending and so continues "


def test_move_that_would_break_a_join_is_declined(tmp_path):
    # moving the figure after b3 would wedge it between b3 and b4, which joins b3
    its = [_t("b1", LONG + "into"), {"id": "f1", "kind": "figure", "markdown": "",
                                     "bbox": [0, 0, 100, 40]},
           {"id": "c1", "kind": "caption", "markdown": "Fig. 1. A caption.", "bbox": [0, 0, 1, 1]},
           _t("b3", "the next item that carries it on and ends."),
           _t("b4", "continues that sentence further.", join_previous="space")]
    b, p = _page(tmp_path, its)
    res = hoist_floats(b)
    assert [i["id"] for i in json.loads(p.read_text())["items"]] == ["f1", "c1", "b1", "b3", "b4"] \
        or any("join_previous" in r["detail"] for r in res)


def test_groups_cannot_trade_places_forever(tmp_path):
    its = [_t("a", LONG + "one"), {"id": "f1", "kind": "figure", "markdown": "", "bbox": [0, 0, 9, 9]},
           _t("b", LONG + "two"), {"id": "f2", "kind": "figure", "markdown": "", "bbox": [0, 0, 9, 9]}]
    b, p = _page(tmp_path, its)
    first = hoist_floats(b)
    assert sum(r["applied"] for r in first) <= 2


def test_repair_builder_constraints_fixes_heading_marker_and_bad_join(tmp_path):
    from paperskill.bundle import repair_builder_constraints
    its = [_t("a", LONG + "ends here."),
           {"id": "h", "kind": "heading", "markdown": "3.7. Possibility of a system",
            "bbox": [0, 0, 9, 9]},
           _t("b", "Starts the new section.", join_previous="space")]
    b, p = _page(tmp_path, its)
    res = repair_builder_constraints(b)
    assert len(res) == 2 and all(r["applied"] for r in res)
    got = {i["id"]: i for i in json.loads(p.read_text())["items"]}
    assert got["h"]["markdown"].startswith("### 3.7.")
    assert got["b"]["join_previous"] is None
    assert repair_builder_constraints(b) == []          # idempotent


def test_dash_encoded_as_e_is_recognised():
    from paperskill.bundle import classify_missing_line
    built = "The filtration rate of the RSF during the study period was 290–470 m/d."
    assert classify_missing_line("period was 290e470 m/d.", built) == "dash_encoded"
    assert classify_missing_line("period was 290e470 m/d.", "nothing relevant here") == "absent"


def test_degree_sign_repair(tmp_path):
    from paperskill.bundle import repair_degree_signs
    its = [{"id": "t", "kind": "table", "markdown": "", "bbox": [0, 0, 9, 9],
            "rows": [["Conductivity (mS/cm at 25 <sup>�</sup>C)", "1"]]}]
    b, p = _page(tmp_path, its)
    assert repair_degree_signs(b)[0]["applied"]
    assert json.loads(p.read_text())["items"][0]["rows"][0][0] == "Conductivity (mS/cm at 25 °C)"


def test_glyph_encoded_control_char_line_is_recognised():
    from paperskill.bundle import classify_missing_line
    built = "Conductivity (µS/cm at 25 °C) | 623 | 611"
    assert classify_missing_line("Conductivity (mS/cm at 25 \u0004C)", built) == "glyph_encoded"
    assert classify_missing_line("Conductivity (mS/cm at 25 \u0004C)", "unrelated text") != "glyph_encoded"


def test_stale_adjudications_are_pruned(tmp_path):
    from paperskill.bundle import prune_stale_adjudications
    adj = tmp_path / "adjudications.json"
    e = lambda fp: {"page": 2, "check": "number_differences", "fingerprint": fp,  # noqa: E731
                    "reason": "tokenization only, same visible content"}
    adj.write_text(json.dumps({"schema_version": 1, "entries": [e("old"), e("new")]}))

    class B:
        plan_dirs = [tmp_path]
        def doc_verification(self, sid):
            return {"stale_adjudications": [{"page": 2, "check": "number_differences",
                                             "fingerprint": "old"}]}
        def adjudications_path(self, sid):
            return adj

    assert prune_stale_adjudications(B()) == 1
    assert [x["fingerprint"] for x in json.loads(adj.read_text())["entries"]] == ["new"]
    assert prune_stale_adjudications(B()) == 0


def test_level_one_headings_and_graphical_abstract_are_repaired(tmp_path):
    from paperskill.bundle import repair_builder_constraints
    its = [{"id": "h1", "kind": "heading", "markdown": "# 3.2. Removal of H5N1", "bbox": [0, 0, 9, 9]},
           {"id": "h2", "kind": "heading", "markdown": "# Evaluation of virus removal", "bbox": [0, 0, 9, 9]},
           _t("l", "G R A P H I C A L  A B S T R A C T"), _t("hl", "- A highlight line, long enough to be prose."),
           {"id": "f", "kind": "figure", "markdown": "", "asset_name": "figure-p0001-018",
            "label": "Figure on PDF page 1 (19)", "bbox": [0, 0, 9, 9]}]
    b, p = _page(tmp_path, its)
    repair_builder_constraints(b)
    got = {i["id"]: i for i in json.loads(p.read_text())["items"]}
    assert got["h1"]["markdown"] == "### 3.2. Removal of H5N1"
    assert got["h2"]["kind"] == "text" and not got["h2"]["markdown"].startswith("#")
    assert got["f"]["asset_name"] == "graphical-abstract"
    assert repair_builder_constraints(b) == []


def test_duplicate_asset_name_from_a_table_split_over_pages_is_renamed(tmp_path):
    from paperskill.bundle import repair_builder_constraints
    its = [{"id": "t1", "kind": "table", "markdown": "", "bbox": [0, 0, 9, 9],
            "asset_name": "table-2", "label": "Table 2"},
           {"id": "t2", "kind": "table", "markdown": "", "bbox": [0, 0, 9, 9],
            "asset_name": "table-2", "label": "Table 2"},
           {"id": "t3", "kind": "table", "markdown": "", "bbox": [0, 0, 9, 9],
            "asset_name": "table-2", "label": "Table 2"}]
    b, p = _page(tmp_path, its)
    res = repair_builder_constraints(b)
    got = {i["id"]: i["asset_name"] for i in json.loads(p.read_text())["items"]}
    assert got == {"t1": "table-2", "t2": "table-2-cont", "t3": "table-2-cont2"}
    assert len(res) == 2 and repair_builder_constraints(b) == []


def test_prune_navigation_drops_entries_the_repairs_made_stale(tmp_path):
    from paperskill.bundle import PAPER_FILE, prune_navigation
    its = [{"id": "h1", "kind": "heading", "markdown": "## Methods", "bbox": [0, 0, 9, 9]},
           {"id": "h2", "kind": "heading", "markdown": "## A wrapped title\nsecond line",
            "bbox": [0, 0, 9, 9]}]
    b, _ = _page(tmp_path, its)
    store = {"navigation": [
        {"file": PAPER_FILE, "heading": "Methods", "purpose": "x"},
        {"file": PAPER_FILE, "heading": "A wrapped title", "purpose": "x"},
        {"file": PAPER_FILE, "heading": "A wrapped title\nsecond line", "purpose": "x"},
        {"file": PAPER_FILE, "heading": "<mark>■</mark>Gone", "purpose": "x"}]}
    b.bundle_json = lambda: store
    b.write_bundle_json = lambda d: store.update(d)
    assert prune_navigation(b) == 2
    assert [e["heading"] for e in store["navigation"]] == ["Methods", "A wrapped title"]
