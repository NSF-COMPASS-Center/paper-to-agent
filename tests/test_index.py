from pathlib import Path

from paperskill.index import archive_path, record_from_report, update_index


def test_archive_path_is_timestamped_under_archive(tmp_path):
    p = archive_path(tmp_path / "paper-3")
    assert p.parent == tmp_path / "_archive" / "paper-3" and p.name[:2] == "20"


def test_index_keeps_one_row_per_paper_latest_wins(tmp_path):
    rep = {"name": "paper-3", "work": "w", "final": {"status": "delivered_draft", "output": "x/paper-3",
           "limitations": [1, 2], "limitations_file": "x/paper-3.LIMITATIONS.md"},
           "metrics": {"tables": 18, "tables_as_images": 14, "cost_usd": 4.1}}
    update_index(tmp_path, record_from_report(rep))
    rep["final"]["status"] = "reviewed"
    rep["final"]["limitations"] = []
    md = update_index(tmp_path, record_from_report(rep))
    text = Path(md).read_text()
    assert sum(l.startswith("| paper-3 |") for l in text.splitlines()) == 1 and "reviewed" in text and "delivered_draft" not in text
