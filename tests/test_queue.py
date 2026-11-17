"""review-queue.json parsing, pinned to the shape review-aid actually emits.

The payloads below were copied from a real run of the vendored
`paper_bundle.py review-aid` on a 13-page journal PDF.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paperskill.review import queue_notes_by_page  # noqa: E402

SID = "s001-0b37c3fa-18-investigation-of-enteric-adenovirus"

REAL_QUEUE = {
    "schema_version": 1,
    "sources": [{
        "id": SID,
        "title": "Investigation of enteric adenovirus ...",
        "pages": 13,
        "contact_sheets": ["review-aid/contact-1.png"],
        "items": [
            {"page": 1, "kind": "unreviewed_page"},
            {"page": 1, "kind": "extractor_warning",
             "detail": "Review possible header/footer: p0001-b000"},
            {"page": 2, "kind": "suggested_compact_omission",
             "item": "p0002-b000", "reason": "Repeated decorative page header/footer."},
            {"page": 4, "kind": "possible_cross_page_join",
             "detail": "p0004-b002 may continue p0003-b014"},
        ],
    }],
}


def test_sources_key_is_read_not_documents():
    idx = queue_notes_by_page(REAL_QUEUE)
    assert (SID, 1) in idx, "parser must key off sources[].id"
    assert (SID, 2) in idx and (SID, 4) in idx


def test_unreviewed_page_is_dropped():
    notes = queue_notes_by_page(REAL_QUEUE)[(SID, 1)]
    assert len(notes) == 1
    assert "unreviewed_page" not in notes[0]
    assert "header/footer" in notes[0]


def test_reason_and_item_reference_are_carried():
    note = queue_notes_by_page(REAL_QUEUE)[(SID, 2)][0]
    assert "suggested_compact_omission" in note
    assert "Repeated decorative" in note
    assert "[p0002-b000]" in note


def test_cross_page_join_flag_reaches_the_page():
    note = queue_notes_by_page(REAL_QUEUE)[(SID, 4)][0]
    assert "possible_cross_page_join" in note and "p0003-b014" in note


def test_empty_and_malformed_input_is_safe():
    assert queue_notes_by_page({}) == {}
    assert queue_notes_by_page({"sources": []}) == {}
    assert queue_notes_by_page({"sources": [{"id": "x"}]}) == {}
    assert queue_notes_by_page(None) == {}          # type: ignore[arg-type]
    assert queue_notes_by_page([]) == {}            # type: ignore[arg-type]


# --- output path rules -------------------------------------------------------
# reading_package.build refuses an output dir that exists, is inside the review
# dir, or contains it. These pin all three so the loop cannot regress into
# spending a paper's worth of tokens and then failing at the build.

def test_output_inside_work_is_rejected(tmp_path):
    from paperskill.coordinator import check_paths
    work = tmp_path / "runs" / "p-review"
    assert "inside --work" in check_paths(work, work / "staging" / "p")
    assert "cannot be the same" in check_paths(work, work)


def test_work_inside_output_is_rejected(tmp_path):
    from paperskill.coordinator import check_paths
    out = tmp_path / "dist"
    assert "inside --out" in check_paths(out / "nested" / "p-review", out)


def test_sibling_paths_are_accepted(tmp_path):
    from paperskill.coordinator import check_paths
    assert check_paths(tmp_path / "runs" / "p-review", tmp_path / "dist" / "p") == ""


def test_fresh_dir_never_returns_an_existing_path(tmp_path):
    from paperskill.coordinator import fresh_dir
    base = tmp_path / "a" / "attempt-1"
    first = fresh_dir(base)
    assert first == base and not first.exists() and base.parent.exists()
    first.mkdir()
    second = fresh_dir(base)
    assert second != first and not second.exists()
    second.mkdir()
    assert fresh_dir(base) not in (first, second)


def test_clear_for_build_moves_an_existing_package_aside(tmp_path):
    from paperskill.coordinator import clear_for_build
    out = tmp_path / "dist" / "pkg"
    (out / "references").mkdir(parents=True)
    (out / "SKILL.md").write_text("v1")
    clear_for_build(out, lambda m: None)
    assert not out.exists()
    archive = tmp_path / "dist" / "_archive" / "pkg"
    (first,) = list(archive.iterdir())
    assert (first / "SKILL.md").read_text() == "v1"
    # a second rebuild does not clobber the first archived build
    out.mkdir(parents=True)
    (out / "SKILL.md").write_text("v2")
    clear_for_build(out, lambda m: None)
    assert (first / "SKILL.md").read_text() == "v1"
    assert sorted(p.name for p in archive.iterdir()) and len(list(archive.iterdir())) == 2
    assert {(p / "SKILL.md").read_text() for p in archive.iterdir()} == {"v1", "v2"}


# --- source-level review roll-up --------------------------------------------
# bundle.json carries its OWN `reviewed` flag per source, separate from the
# per-page flags, and `paper_bundle.load_bundle` rejects a reviewed source whose
# review_notes are empty. Nothing was setting it, so a fully page-reviewed paper
# still failed the build.

def _fake_bundle(tmp_path, sid="s001-x", pages=3, reviewed=True):
    import json as _json
    work = tmp_path / "review"
    (work / "documents" / sid / "pages").mkdir(parents=True)
    for n in range(1, pages + 1):
        (work / "documents" / sid / "pages" / f"page-{n:04d}.json").write_text(
            _json.dumps({"page": n, "reviewed": reviewed, "review_notes": "x" * 30,
                         "items": []}))
    (work / "bundle.json").write_text(_json.dumps({
        "schema_version": 2, "name": "x", "title": "X", "notes": [],
        "sources": [{"id": sid, "role": "main", "title": "X",
                     "reviewed": False, "review_notes": "", "asset_name": "x"}]}))
    return work


def test_source_is_marked_reviewed_with_factual_notes(tmp_path):
    import json as _json
    from paperskill.bundle import Bundle
    from paperskill.config import Config
    from paperskill.coordinator import roll_up_source_review
    work = _fake_bundle(tmp_path)
    b = Bundle(work, Config())
    recs = [{"source": "s001-x", "page": 1, "status": "reviewed",
             "ops": ["reorder(5 items)", "set_markdown(a, 3->4 chars, @1)"],
             "limitations": ["Table 2 merged headers could not be flattened"]},
            {"source": "s001-x", "page": 2, "status": "reviewed",
             "ops": ["reorder(2 items)"], "limitations": []},
            {"source": "s001-x", "page": 3, "status": "reviewed", "ops": [], "limitations": []}]
    pending = roll_up_source_review(b, recs, Config(), lambda m: None)
    assert pending == []
    src = _json.loads((work / "bundle.json").read_text())["sources"][0]
    assert src["reviewed"] is True
    assert src["review_notes"].strip()                      # load_bundle requires this
    assert "All 3 pages" in src["review_notes"]
    assert "reorder: 2" in src["review_notes"]              # counted from the op log
    assert "set_markdown: 1" in src["review_notes"]
    assert "merged headers" in src["review_notes"]          # limitation carried through
    assert "not independently certified" in src["review_notes"]


def test_source_stays_unreviewed_while_any_page_is(tmp_path):
    import json as _json
    from paperskill.bundle import Bundle
    from paperskill.config import Config
    from paperskill.coordinator import roll_up_source_review
    work = _fake_bundle(tmp_path, reviewed=False)
    b = Bundle(work, Config())
    pending = roll_up_source_review(b, [], Config(), lambda m: None)
    assert pending == ["s001-x"]
    assert _json.loads((work / "bundle.json").read_text())["sources"][0]["reviewed"] is False


def test_a_source_with_no_page_plans_is_not_claimed_as_reviewed(tmp_path):
    import json as _json
    from paperskill.bundle import Bundle
    from paperskill.config import Config
    from paperskill.coordinator import roll_up_source_review
    work = tmp_path / "review"
    (work / "documents").mkdir(parents=True)
    (work / "bundle.json").write_text(_json.dumps({
        "schema_version": 2, "name": "x", "title": "X", "notes": [],
        "sources": [{"id": "s002-sheet", "role": "tables", "title": "T",
                     "reviewed": False, "review_notes": "", "asset_name": "t"}]}))
    msgs = []
    pending = roll_up_source_review(Bundle(work, Config()), [], Config(), msgs.append)
    assert pending == ["s002-sheet"]
    assert any("no page plans" in m for m in msgs)
    assert _json.loads((work / "bundle.json").read_text())["sources"][0]["reviewed"] is False


# --- diagnostic collection ---------------------------------------------------
# The review queue reports only a check name and a fingerprint (detail is null).
# The real evidence -- which number tokens disagree -- is in
# documents/<sid>/verification.json. Joining the two is what lets a diagnostic be
# adjudicated at all; without it the adjudicator can only say "repair needed",
# which never converges because a tokenization difference cannot be re-read away.

def _diag_fixture(tmp_path):
    import json as _json
    sid = "s001-x"
    work = tmp_path / "review"
    (work / "documents" / sid / "pages").mkdir(parents=True)
    (work / "review-aid").mkdir(parents=True)
    (work / "documents" / sid / "pages" / "page-0002.json").write_text(_json.dumps(
        {"page": 2, "reviewed": True, "review_notes": "x" * 30, "items": [
            {"id": "p0002-b004", "kind": "text",
             "markdown": "spiked at 10<sup>11</sup> copies/L in each experiment"}]}))
    (work / "documents" / sid / "verification.json").write_text(_json.dumps(
        {"status": "unresolved_discrepancies", "pages": [
            {"page": 1, "number_differences": {"missing": {}, "extra": {}},
             "missing_lines": []},
            {"page": 2,
             "number_differences": {"missing": {"1011": 1}, "extra": {"10": 1, "11": 1}},
             "independent_parser_number_differences": {"missing": {}, "extra": {}},
             "missing_lines": []}]}))
    (work / "review-aid" / "review-queue.json").write_text(_json.dumps(
        {"schema_version": 1, "sources": [{"id": sid, "items": [
            {"page": 2, "kind": "unresolved_diagnostic",
             "adjudication_entry": {"page": 2, "check": "number_differences",
                                    "fingerprint": "e743e9cc", "reason": "PLACEHOLDER"}},
            {"page": 2, "kind": "extractor_warning", "detail": "no entry"}]}]}))
    return work, sid


def test_diagnostics_are_joined_to_their_real_token_evidence(tmp_path):
    from paperskill.bundle import Bundle, collect_diagnostics
    from paperskill.config import Config
    work, sid = _diag_fixture(tmp_path)
    ds = collect_diagnostics(Bundle(work, Config()))
    assert len(ds) == 1, "only queue items carrying an adjudication_entry count"
    d = ds[0]
    assert d["page"] == 2 and d["check"] == "number_differences"
    # the evidence the adjudicator could not see before
    assert d["numbers_in_source_not_in_output"] == {"1011": 1}
    assert d["numbers_in_output_not_in_source"] == {"10": 1, "11": 1}
    # and the page text that explains it: a superscript power of ten
    assert "10<sup>11</sup>" in d["built_page_text"]
    # the fingerprint must survive verbatim or the adjudication goes stale
    assert d["adjudication_entry"]["fingerprint"] == "e743e9cc"


def test_diagnostics_survive_a_missing_verification_report(tmp_path):
    from paperskill.bundle import Bundle, collect_diagnostics
    from paperskill.config import Config
    work, sid = _diag_fixture(tmp_path)
    (work / "documents" / sid / "verification.json").unlink()
    ds = collect_diagnostics(Bundle(work, Config()))
    assert len(ds) == 1
    assert ds[0]["numbers_in_source_not_in_output"] == {}   # empty, not a crash
    assert "10<sup>11</sup>" in ds[0]["built_page_text"]


def test_no_queue_means_no_diagnostics(tmp_path):
    from paperskill.bundle import Bundle, collect_diagnostics
    from paperskill.config import Config
    (tmp_path / "review").mkdir()
    assert collect_diagnostics(Bundle(tmp_path / "review", Config())) == []


# --- mangled comparison symbols ---------------------------------------------
# Measured on a real Elsevier PDF: ">" extracts as "N" and "<" as "b" before a
# number, sometimes with a space ("P N 0.63"). On one 13-page run the review
# restored four of six and DELETED two, turning "2.4->3.9-log10" (open upper
# bound) into "2.4-3.9-log10" (closed range) -- a change to what the paper
# reports that verification cannot see, because the raw line counts as missing
# either way.
#
# Classification compares the NUMBER the symbol qualifies, not the surrounding
# text, because a review legitimately changes other characters on the same line
# (log10 -> log subscript, FeCl3 -> FeCl subscript).

BUILT_P4 = (
    "conditions to optimize turbidity removal (76\u201399%) and minimize the residual "
    "aluminum concentration. size of the membrane filter (0.45 \u03bcm), no removal "
    "(<0.2-log10) of AdV or PV was observed. by approximately 2\u20133-log10: "
    "1.9\u20133.7-log10 and 2.4\u20133.9-log10 removals of AdV and PV")
BUILT_P5 = ("(Fig. 4). In particular, coagulation with FeCl\u2083 resulted in "
            ">4-log\u2081\u2080 removals of PV from all the source waters")
BUILT_P6 = ("d by filtration (analysis of covariance, P > 0.63); whereas the ratios "
            "after settling and after filtration regardless of the coagulant type "
            "(P < 0.02) (Table S2)")


def test_deleted_comparison_symbol_is_caught():
    from paperskill.bundle import classify_missing_line
    assert classify_missing_line(
        "conditions to optimize turbidity removal (76\u2013N99%) and minimize",
        BUILT_P4) == "symbol_dropped"
    assert classify_missing_line(
        "by approximately 2\u20133-log10: 1.9\u2013N3.7-log10 and 2.4\u2013N3.9-log10 re-",
        BUILT_P4) == "symbol_dropped"


def test_correctly_restored_symbol_is_not_a_defect():
    from paperskill.bundle import classify_missing_line
    assert classify_missing_line(
        "size of the membrane \ufb01lter (0.45 \u03bcm), no removal (b0.2-log10) of AdV",
        BUILT_P4) == "restored"


def test_restoration_survives_other_edits_on_the_same_line():
    """The review also rewrote log10 and FeCl3 with subscripts; comparing the
    surrounding text would call this a false defect."""
    from paperskill.bundle import classify_missing_line
    assert classify_missing_line(
        "ticular, coagulation with FeCl3 resulted in N4-log10 removals of PV",
        BUILT_P5) == "restored"


def test_symbol_separated_from_its_number_by_a_space_is_handled():
    """`P N 0.63` is `P > 0.63`; requiring the digit to be adjacent missed these."""
    from paperskill.bundle import classify_missing_line
    assert classify_missing_line(
        "and settling were followed by \ufb01ltration (analysis of covariance, P N 0.63);",
        BUILT_P6) == "restored"
    assert classify_missing_line(
        "tling and after \ufb01ltration regardless of the coagulant type (P b 0.02)",
        BUILT_P6) == "restored"


def test_absent_and_other_are_distinguished():
    from paperskill.bundle import classify_missing_line
    assert classify_missing_line("a line that vanished", BUILT_P4) == "absent"
    assert classify_missing_line("aluminum concentration.", BUILT_P4) == "other"


def test_worst_verdict_wins_when_a_line_has_several_symbols():
    from paperskill.bundle import classify_missing_line
    built = "ratio was <0.5 and the range was 1.0-2.0 overall"
    raw = "ratio was b0.5 and the range was 1.0-N2.0 overall"
    assert classify_missing_line(raw, built) == "symbol_dropped"


def test_real_letters_before_digits_are_not_treated_as_symbols():
    """An unchanged line short-circuits: the address N13W8 and a label like
    `sample N3` both match the pattern but nothing was dropped."""
    from paperskill.bundle import classify_missing_line
    addr = "Hokkaido University, N13W8, Sapporo 060-8628, Japan"
    assert classify_missing_line(addr, addr) == "other"
    assert classify_missing_line("sample N3 and tube b7 were used",
                                 "sample N3 and tube b7 were used") == "other"


def test_check_symbol_repairs_walks_the_review(tmp_path):
    import json as _json
    from paperskill.bundle import Bundle, check_symbol_repairs
    from paperskill.config import Config
    sid = "s001-x"
    work = tmp_path / "review"
    (work / "documents" / sid / "pages").mkdir(parents=True)
    (work / "documents" / sid / "pages" / "page-0004.json").write_text(_json.dumps(
        {"page": 4, "reviewed": True, "review_notes": "x" * 30,
         "items": [{"id": "a", "kind": "text", "markdown": BUILT_P4}]}))
    (work / "documents" / sid / "verification.json").write_text(_json.dumps(
        {"pages": [{"page": 4, "missing_lines": [
            {"text": "conditions to optimize turbidity removal (76\u2013N99%) and minimize"},
            {"text": "no removal (b0.2-log10) of AdV"}]}]}))
    res = check_symbol_repairs(Bundle(work, Config()))
    assert sorted(r["verdict"] for r in res) == ["restored", "symbol_dropped"]
    assert all(r["page"] == 4 and r["source_id"] == sid for r in res)


def test_dropped_symbols_are_repaired_deterministically(tmp_path):
    """Two agent passes, given the rule explicitly, left these out. The repair is
    mechanical -- the raw extraction says which symbol belongs before which number
    -- so it is applied in Python and verified by re-running the check."""
    import json as _json
    from paperskill.bundle import Bundle, check_symbol_repairs, repair_dropped_symbols
    from paperskill.config import Config
    sid, work = "s1", tmp_path / "review"
    (work / "documents" / sid / "pages").mkdir(parents=True)
    items = [
        {"id": "p4-a", "kind": "text",
         "markdown": "imize turbidity removal (76–99%) and minimize the residual"},
        {"id": "p4-b", "kind": "text",
         "markdown": "no removal (<0.2-log10) of AdV or PV was observed."},
        {"id": "p4-c", "kind": "text",
         "markdown": "by approximately 2–3-log10: 1.9–3.7-log10 and "
                     "2.4–3.9-log10 removals"},
    ]
    (work / "documents" / sid / "pages" / "page-0004.json").write_text(_json.dumps(
        {"page": 4, "width": 595, "height": 842, "reviewed": True,
         "review_notes": "x" * 30, "items": items}))
    (work / "documents" / sid / "verification.json").write_text(_json.dumps(
        {"pages": [{"page": 4, "missing_lines": [
            {"text": "conditions to optimize turbidity removal (76–N99%) and minimize"},
            {"text": "no removal (b0.2-log10) of AdV"},
            {"text": "by approximately 2–3-log10: 1.9–N3.7-log10 and "
                     "2.4–N3.9-log10 re-"}]}]}))
    b = Bundle(work, Config())
    assert [r["verdict"] for r in check_symbol_repairs(b)] == [
        "symbol_dropped", "restored", "symbol_dropped"]

    fixes = repair_dropped_symbols(b)
    assert sum(1 for f in fixes if f["applied"]) == 3     # one line carried two symbols
    assert all(r["verdict"] != "symbol_dropped" for r in check_symbol_repairs(b))

    out = _json.loads((work / "documents" / sid / "pages" / "page-0004.json").read_text())
    md = " ".join(i["markdown"] for i in out["items"])
    assert "(76–>99%)" in md
    assert "1.9–>3.7-log10" in md and "2.4–>3.9-log10" in md
    assert "(<0.2-log10)" in md                            # untouched, already correct
    assert "Deterministic repair" in out["review_notes"]    # disclosed, not silent


def test_dry_run_reports_without_writing(tmp_path):
    import json as _json
    from paperskill.bundle import Bundle, repair_dropped_symbols
    from paperskill.config import Config
    sid, work = "s1", tmp_path / "review"
    (work / "documents" / sid / "pages").mkdir(parents=True)
    page = {"page": 4, "width": 595, "height": 842, "reviewed": True,
            "review_notes": "x" * 30,
            "items": [{"id": "a", "kind": "text", "markdown": "range was 1.0–2.0 here"}]}
    (work / "documents" / sid / "pages" / "page-0004.json").write_text(_json.dumps(page))
    (work / "documents" / sid / "verification.json").write_text(_json.dumps(
        {"pages": [{"page": 4, "missing_lines": [{"text": "range was 1.0–N2.0 here"}]}]}))
    before = (work / "documents" / sid / "pages" / "page-0004.json").read_text()
    res = repair_dropped_symbols(Bundle(work, Config()), apply=False)
    assert res and all(not r["applied"] for r in res)
    assert "would insert" in res[0]["detail"]
    assert (work / "documents" / sid / "pages" / "page-0004.json").read_text() == before


def test_ambiguous_anchor_is_refused_not_guessed(tmp_path):
    """If the anchor text matches more than once, inserting a symbol could change
    the wrong number. Report it instead."""
    import json as _json
    from paperskill.bundle import Bundle, repair_dropped_symbols
    from paperskill.config import Config
    sid, work = "s1", tmp_path / "review"
    (work / "documents" / sid / "pages").mkdir(parents=True)
    (work / "documents" / sid / "pages" / "page-0004.json").write_text(_json.dumps(
        {"page": 4, "width": 595, "height": 842, "reviewed": True, "review_notes": "x" * 30,
         "items": [{"id": "a", "kind": "text",
                    "markdown": "was 1.0–2.0 and was 1.0–2.0 again"}]}))
    (work / "documents" / sid / "verification.json").write_text(_json.dumps(
        {"pages": [{"page": 4, "missing_lines": [{"text": "was 1.0–N2.0 and"}]}]}))
    res = repair_dropped_symbols(Bundle(work, Config()))
    assert res and not any(r["applied"] for r in res)
    assert "ambiguous" in res[0]["detail"]


# --- adjudications.json schema ----------------------------------------------
# `pdf_to_skill.load_adjudications` requires {"schema_version": 1, "entries": [...]}
# and calls data.get(...) on it, so writing a bare list made the build die with
# "AttributeError: 'list' object has no attribute 'get'" -- an error that names no
# page and no file. Each entry must also have a positive int page, a check from a
# fixed set, a 64-hex fingerprint and a non-placeholder reason.

FP_A, FP_B, FP_C = "a" * 64, "b" * 64, "c" * 64


def _bundle(tmp_path):
    from paperskill.bundle import Bundle
    from paperskill.config import Config
    (tmp_path / "review" / "documents" / "s1").mkdir(parents=True)
    return Bundle(tmp_path / "review", Config())


def test_adjudications_are_written_in_the_engines_schema(tmp_path):
    import json as _json
    from paperskill.bundle import write_adjudications
    b = _bundle(tmp_path)
    skipped = write_adjudications(b, "s1", [
        {"page": 2, "check": "number_differences", "fingerprint": FP_A,
         "reason": "10 with superscript 11 read as 1011 by one parser"}])
    assert skipped == []
    data = _json.loads(b.adjudications_path("s1").read_text())
    assert data["schema_version"] == 1
    assert isinstance(data["entries"], list) and len(data["entries"]) == 1
    assert set(data["entries"][0]) == {"page", "check", "fingerprint", "reason"}


def test_a_legacy_bare_list_file_is_migrated(tmp_path):
    import json as _json
    from paperskill.bundle import write_adjudications
    b = _bundle(tmp_path)
    b.adjudications_path("s1").write_text(_json.dumps(
        [{"page": 2, "check": "number_differences", "fingerprint": FP_B,
          "reason": "written before the schema was right"}]))
    write_adjudications(b, "s1", [
        {"page": 3, "check": "missing_lines", "fingerprint": FP_A, "reason": "repair"}])
    data = _json.loads(b.adjudications_path("s1").read_text())
    assert data["schema_version"] == 1
    assert sorted(e["page"] for e in data["entries"]) == [2, 3]


def test_invalid_entries_are_skipped_with_reasons_not_written(tmp_path):
    import json as _json
    from paperskill.bundle import write_adjudications
    b = _bundle(tmp_path)
    skipped = write_adjudications(b, "s1", [
        {"page": 3, "check": "number_differences", "fingerprint": FP_A, "reason": "ok"},
        {"page": 4, "check": "not_a_check", "fingerprint": FP_A, "reason": "x"},
        {"page": 5, "check": "missing_lines", "fingerprint": "short", "reason": "y"},
        {"page": 6, "check": "missing_lines", "fingerprint": FP_C,
         "reason": "Describe the visible source check."},
        {"page": 0, "check": "missing_lines", "fingerprint": FP_C, "reason": "z"},
    ])
    assert len(skipped) == 4
    assert any("check must be one of" in s for s in skipped)
    assert any("64 lowercase hex" in s for s in skipped)
    assert any("placeholder" in s for s in skipped)
    assert any("positive int" in s for s in skipped)
    entries = _json.loads(b.adjudications_path("s1").read_text())["entries"]
    assert [e["page"] for e in entries] == [3]


def test_duplicate_keys_are_merged_not_repeated(tmp_path):
    import json as _json
    from paperskill.bundle import write_adjudications
    b = _bundle(tmp_path)
    e = {"page": 2, "check": "number_differences", "fingerprint": FP_A, "reason": "first"}
    write_adjudications(b, "s1", [e])
    write_adjudications(b, "s1", [dict(e, reason="second")])
    entries = _json.loads(b.adjudications_path("s1").read_text())["entries"]
    assert len(entries) == 1 and entries[0]["reason"] == "second"


def test_valid_adjudication_accepts_a_good_entry():
    from paperskill.bundle import valid_adjudication
    assert valid_adjudication({"page": 1, "check": "missing_lines",
                               "fingerprint": FP_A, "reason": "a real reason"}) == ""


def test_normalizing_with_no_new_entries_repairs_the_file(tmp_path):
    """`load_adjudications` runs at the start of every build, so a malformed file
    fails attempt 1 -- before the adjudication step that would have rewritten it.
    Calling write_adjudications with no new entries must repair it in place."""
    import json as _json
    from paperskill.bundle import write_adjudications
    b = _bundle(tmp_path)
    b.adjudications_path("s1").write_text(_json.dumps(
        [{"page": 2, "check": "number_differences", "fingerprint": FP_A, "reason": "r1"},
         {"page": 3, "check": "number_differences", "fingerprint": FP_B, "reason": "r2"}]))
    skipped = write_adjudications(b, "s1", [])
    assert skipped == []
    data = _json.loads(b.adjudications_path("s1").read_text())
    assert data["schema_version"] == 1
    assert sorted(e["page"] for e in data["entries"]) == [2, 3]



# --- navigation validation ---------------------------------------------------
# `index_text` rejects a navigation entry whose heading is not present verbatim in
# the built document, and that stops the whole build. Synthesis runs before the
# build, so the proposal is checked against the headings the page plans will
# produce.

def test_navigation_heading_must_exist_verbatim(tmp_path):
    from paperskill.bundle import PAPER_FILE, validate_navigation
    hs = {PAPER_FILE: [{"file": PAPER_FILE, "level": 2, "heading": "1. Introduction",
                        "page": 2}],
          "references/supplement.md": []}
    assert validate_navigation(
        [{"file": PAPER_FILE, "heading": "1. Introduction", "purpose": "background"}],
        hs) == []
    problems = validate_navigation(
        [{"file": PAPER_FILE, "heading": "1 Introduction", "purpose": "typo"}], hs)
    assert problems and "not in" in problems[0]


def test_navigation_rejects_unknown_file_and_duplicates(tmp_path):
    from paperskill.bundle import PAPER_FILE, validate_navigation
    hs = {PAPER_FILE: [{"file": PAPER_FILE, "level": 2, "heading": "Abstract",
                        "page": 1}], "references/supplement.md": []}
    assert any("file must be" in p for p in validate_navigation(
        [{"file": "references/other.md", "heading": "Abstract", "purpose": "x"}], hs))
    dup = validate_navigation(
        [{"file": PAPER_FILE, "heading": "Abstract", "purpose": "x"},
         {"file": PAPER_FILE, "heading": "Abstract", "purpose": "y"}], hs)
    assert any("duplicate" in p for p in dup)


def test_navigation_entry_cap_is_enforced(tmp_path):
    from paperskill.bundle import NAV_MAX_ENTRIES, PAPER_FILE, validate_navigation
    hs = {PAPER_FILE: [{"file": PAPER_FILE, "level": 2, "heading": f"S{i}", "page": i}
                       for i in range(NAV_MAX_ENTRIES + 5)],
          "references/supplement.md": []}
    entries = [{"file": PAPER_FILE, "heading": f"S{i}", "purpose": "p"}
               for i in range(NAV_MAX_ENTRIES + 5)]
    assert any("exceeds the" in p for p in validate_navigation(entries, hs))


def test_document_headings_parses_depth_and_routes_by_role(tmp_path):
    import json as _json
    from paperskill.bundle import Bundle, PAPER_FILE, SUPPLEMENT_FILE, document_headings
    from paperskill.config import Config
    work = tmp_path / "review"
    for sid, role in (("s001-main", "main"), ("s002-supp", "supplement")):
        (work / "documents" / sid / "pages").mkdir(parents=True)
        (work / "documents" / sid / "pages" / "page-0001.json").write_text(_json.dumps(
            {"page": 1, "reviewed": True, "review_notes": "x" * 30, "items": [
                {"id": "a", "kind": "heading", "markdown": "## Abstract"},
                {"id": "b", "kind": "heading", "markdown": "### 2.1. Source water"},
                {"id": "c", "kind": "text", "markdown": "## not a heading item"}]}))
    (work / "bundle.json").write_text(_json.dumps({"sources": [
        {"id": "s001-main", "role": "main"}, {"id": "s002-supp", "role": "supplement"}]}))
    hs = document_headings(Bundle(work, Config()))
    assert [(h["level"], h["heading"]) for h in hs[PAPER_FILE]] == [
        (2, "Abstract"), (3, "2.1. Source water")]
    assert len(hs[SUPPLEMENT_FILE]) == 2
    assert all(h["heading"] != "not a heading item" for hl in hs.values() for h in hl)
