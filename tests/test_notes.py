import json

from paperskill.bundle import Bundle
from paperskill.config import Config
from paperskill.notes import page_review_notes, render_notes, write_notes


def work_with_notes(tmp_path, notes):
    work = tmp_path / "work"
    (work / "documents" / "s001" / "pages").mkdir(parents=True)
    (work / "bundle.json").write_text(json.dumps({"name": "x", "notes": notes, "sources": []}))
    for n, text in ((1, "Confirmed reading order."), (2, ""), (3, "Table 2 transcribed.")):
        (work / "documents" / "s001" / "pages" / f"page-{n:04d}.json").write_text(
            json.dumps({"page": n, "review_notes": text, "items": []}))
    return work


class Proc:
    returncode, stdout, stderr = 0, "{}", ""


def spy(work, seen, boom=False):
    def run(cmd, **kw):
        seen.append((cmd[-1], json.loads((work / "bundle.json").read_text())["notes"]))
        if boom:
            raise RuntimeError("engine blew up")
        return Proc()
    return run


def test_build_verify_and_review_aid_see_no_notes_and_notes_come_back(tmp_path, monkeypatch):
    work = work_with_notes(tmp_path, ["Figure 2 was not extracted."])
    seen = []
    monkeypatch.setattr("paperskill.bundle.subprocess.run", spy(work, seen))
    b = Bundle(work, Config())
    b.build(tmp_path / "out")
    b.verify(strict=True)
    b.review_aid()
    assert [n for _, n in seen] == [[], [], []]
    assert json.loads((work / "bundle.json").read_text())["notes"] == ["Figure 2 was not extracted."]


def test_other_commands_see_the_notes(tmp_path, monkeypatch):
    work = work_with_notes(tmp_path, ["keep"])
    seen = []
    monkeypatch.setattr("paperskill.bundle.subprocess.run", spy(work, seen))
    Bundle(work, Config())._run("prepare", "--work", str(work))
    assert seen[0][1] == ["keep"]


def test_notes_are_restored_even_when_the_command_raises(tmp_path, monkeypatch):
    work = work_with_notes(tmp_path, ["keep me"])
    monkeypatch.setattr("paperskill.bundle.subprocess.run", spy(work, [], boom=True))
    try:
        Bundle(work, Config()).build(tmp_path / "out")
    except RuntimeError:
        pass
    assert json.loads((work / "bundle.json").read_text())["notes"] == ["keep me"]


def test_notes_stay_in_the_package_when_asked(tmp_path, monkeypatch):
    work = work_with_notes(tmp_path, ["a note"])
    cfg = Config()
    cfg.notes_in_package = True
    seen = []
    monkeypatch.setattr("paperskill.bundle.subprocess.run", spy(work, seen))
    Bundle(work, cfg).build(tmp_path / "out")
    assert seen[0][1] == ["a note"]


def test_a_command_that_rewrites_bundle_json_keeps_its_changes_and_the_notes(tmp_path, monkeypatch):
    work = work_with_notes(tmp_path, ["a note"])

    def run(cmd, **kw):
        d = json.loads((work / "bundle.json").read_text())
        d["title"] = "Rewritten by the engine"
        (work / "bundle.json").write_text(json.dumps(d))
        return Proc()
    monkeypatch.setattr("paperskill.bundle.subprocess.run", run)
    Bundle(work, Config()).review_aid()
    after = json.loads((work / "bundle.json").read_text())
    assert after["title"] == "Rewritten by the engine" and after["notes"] == ["a note"]


def test_a_bundle_without_notes_is_left_alone(tmp_path, monkeypatch):
    work = work_with_notes(tmp_path, [])
    before = (work / "bundle.json").read_text()
    monkeypatch.setattr("paperskill.bundle.subprocess.run", spy(work, []))
    Bundle(work, Config()).build(tmp_path / "out")
    assert (work / "bundle.json").read_text() == before


def test_page_notes_skip_empty_and_keep_order(tmp_path):
    work = work_with_notes(tmp_path, [])
    got = page_review_notes([work / "documents" / "s001"])
    assert [(g["page"], g["notes"]) for g in got] == [(1, "Confirmed reading order."),
                                                      (3, "Table 2 transcribed.")]


def test_notes_file_lands_beside_the_package_not_inside_it(tmp_path):
    work = work_with_notes(tmp_path, ["DRAFT: one issue", "Figure 2 was not extracted."])
    out = tmp_path / "dist" / "paper-15"
    out.mkdir(parents=True)
    path = write_notes(out, "paper-15", ["DRAFT: one issue", "Figure 2 was not extracted."],
                       [work / "documents" / "s001"])
    assert path == tmp_path / "dist" / "paper-15.NOTES.md"
    assert not list(out.iterdir())                     # nothing added to the package
    text = path.read_text()
    assert "- DRAFT: one issue" in text and "### s001, page 3" in text
    assert "page 2" not in text


def test_empty_notes_say_so():
    text = render_notes("x", [], [])
    assert text.count("None recorded.") == 2
