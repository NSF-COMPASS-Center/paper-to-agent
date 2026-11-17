import json

from paperskill.bundle import _is_sentence_fragment, repair_builder_constraints, repair_cross_page_joins


class StubBundle:
    def __init__(self, root):
        self.root = root
        self.sd = root / "s001"
        (self.sd / "pages").mkdir(parents=True)

    @property
    def plan_dirs(self):
        return [self.sd]

    def pages(self, sid):
        return sorted((self.sd / "pages").glob("page-*.json"))

    def add(self, n, items):
        (self.sd / "pages" / f"page-{n:04d}.json").write_text(json.dumps(
            {"page": n, "reviewed": True, "review_notes": "reviewed carefully, nothing odd here",
             "items": items}))

    def item(self, n):
        return json.loads((self.sd / "pages" / f"page-{n:04d}.json").read_text())["items"]


def test_fragment_detection():
    assert _is_sentence_fragment("## procedure and Oracle Crystal Ball software version 11.1.2.3. change (as")
    assert not _is_sentence_fragment("## 3.7. Statistical analysis of the collected samples in the study")
    assert not _is_sentence_fragment("## Results")


def test_join_across_a_page_break_after_a_heading_is_cleared(tmp_path):
    b = StubBundle(tmp_path)
    b.add(1, [{"id": "a", "kind": "heading", "markdown": "## 2. Methods"}])
    b.add(2, [{"id": "b", "kind": "text", "markdown": "continues here", "join_previous": "space"}])
    res = repair_cross_page_joins(b)
    assert len(res) == 1 and res[0]["applied"]
    assert b.item(2)[0].get("join_previous") is None
    assert repair_cross_page_joins(b) == []


def test_a_valid_cross_page_join_is_kept(tmp_path):
    b = StubBundle(tmp_path)
    b.add(1, [{"id": "a", "kind": "text", "markdown": "the sentence runs on and"}])
    b.add(2, [{"id": "b", "kind": "text", "markdown": "continues here", "join_previous": "space"}])
    assert repair_cross_page_joins(b) == []


def test_mid_sentence_heading_is_demoted_to_text(tmp_path):
    b = StubBundle(tmp_path)
    b.add(1, [{"id": "a", "kind": "heading",
               "markdown": "## procedure and Oracle Crystal Ball software version 11.1.2.3. change (as"}])
    repair_builder_constraints(b)
    it = b.item(1)[0]
    assert it["kind"] == "text" and not it["markdown"].startswith("#")
