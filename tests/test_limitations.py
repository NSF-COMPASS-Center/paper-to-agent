from paperskill.limitations import (DRAFT_PREFIX, bundle_notes, draft_limitations,
                                    merge_notes, render_markdown)

DIAG = {
    "source_id": "paper", "page": 27, "check": "number_differences",
    "missing_lines_classified": [{"raw": "n = 90. 159", "verdict": "absent"}],
    "numbers_in_source_not_in_output": {"158": 1, "159": 1},
    "numbers_in_output_not_in_source": {},
}


def test_diagnostic_becomes_a_readable_limitation():
    (lim,) = draft_limitations([DIAG])
    assert lim["page"] == 27 and lim["source"] == "paper"
    assert "159" in lim["detail"] and "absent" in lim["detail"]


def test_delivery_fail_is_kept_and_ok_is_dropped():
    lims = draft_limitations([], delivery=[
        {"status": "fail", "check": "caption", "detail": "figure-p0009-002 has no caption"},
        {"status": "ok", "check": "index", "detail": "fine"}])
    assert [l["check"] for l in lims] == ["caption"]


def test_notes_are_prefixed_and_capped():
    lims = draft_limitations([DIAG] * 20)
    notes = bundle_notes(lims)
    assert all(n.startswith(DRAFT_PREFIX) for n in notes)
    assert len(notes) == 1 + 12 + 1          # header, capped list, overflow pointer


def test_merge_replaces_earlier_draft_notes():
    lims = draft_limitations([DIAG])
    once = merge_notes(["keep me"], lims)
    twice = merge_notes(once, lims)
    assert twice == once and twice[0] == "keep me"


def test_no_limitations_adds_no_notes():
    assert bundle_notes([]) == [] and merge_notes(["a"], []) == ["a"]


def test_markdown_names_page_and_reason():
    md = render_markdown("paper-3", draft_limitations([DIAG]), "diagnostic survived")
    assert "DRAFT" in md and "page 27" in md and "diagnostic survived" in md
