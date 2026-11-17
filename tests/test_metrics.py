import json

from paperskill.metrics import flags, paper_metrics, render_table


def _plan(tmp_path, items):
    sd = tmp_path / "s001"
    (sd / "pages").mkdir(parents=True)
    (sd / "pages" / "page-0001.json").write_text(json.dumps({"page": 1, "items": items}))
    return [sd]


def test_counts_come_from_the_report_and_the_plans(tmp_path):
    dirs = _plan(tmp_path, [{"kind": "table", "rows": None}, {"kind": "table", "rows": [["a"]]}])
    report = {"final": {"status": "delivered_draft", "limitations": [1, 2], "delivery_fails": 1},
              "stages": {"crop_repairs": [{"applied": True}, {"applied": False}],
                         "float_hoists": [{"applied": False}],
                         "word_accounting": [{"causes": {"omitted": 12}}]},
              "cost": {"cumulative_all_runs": {"usd": 3.5}}}
    m = paper_metrics(report, dirs)
    assert (m["tables"], m["tables_as_images"]) == (2, 1)
    assert (m["crops_grown"], m["crops_unresolved"], m["lost_words"]) == (1, 1, 12)
    assert m["draft"] and m["cost_usd"] == 3.5


def test_flags_and_table_render(tmp_path):
    m = paper_metrics({"final": {"status": "delivered_draft"},
                       "stages": {"crop_repairs": [{"applied": False}]}}, [])
    assert flags(m) == ["draft", "crop unresolved"]
    assert "delivered_draft" in render_table([("p", m)])
