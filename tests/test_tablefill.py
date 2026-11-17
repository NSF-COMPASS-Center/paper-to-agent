import asyncio
import json
from types import SimpleNamespace

from paperskill import tablefill
from paperskill.editor import PageEditor


def plan(tmp_path, items):
    sd = tmp_path / "s001"
    (sd / "pages").mkdir(parents=True)
    (sd / "source.pdf").write_bytes(b"%PDF-1.4")
    pp = sd / "pages" / "page-0003.json"
    pp.write_text(json.dumps({"page": 3, "reviewed": True, "review_notes": "Confirmed two-column reading order and captions.",
                              "items": items}))
    return sd, pp


def table(i="p0003-b001", **kw):
    return {"id": i, "kind": "table", "bbox": [10, 10, 200, 100], "rows": None,
            "label": "Table 2", "asset_name": "table-2",
            "image_fallback_reason": "Unable to transcribe within budget", **kw}


def rec(verdict="accepted", coverage=1.0, rows=(("a", "b"), ("1", "2")), **kw):
    return {"rows": [list(r) for r in rows], "verdict": verdict, "cut_off": [],
            "uncertain": [], "snaps": [], "paired": [], "unmatched": [],
            "check": {"checked": True, "coverage": coverage, "precision": 1.0,
                      "missing": [], "invented": [], "excluded_lines": []}, **kw}


def run(tmp_path, items, results, monkeypatch):
    sd, pp = plan(tmp_path, items)
    calls = []

    async def fake(client, model, pdf, page, box, dpi, ledger=None, unit="-"):
        calls.append(unit)
        r = results[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(tablefill, "transcribe_table", fake)
    cfg = SimpleNamespace(vision_model="m", transcribe_dpi=400, table_min_coverage=0.5)
    out = asyncio.run(tablefill.fill_image_tables([sd], cfg, None, None))
    return pp, out, calls


def test_an_accepted_read_becomes_rows_and_drops_the_stale_reason(tmp_path, monkeypatch):
    pp, out, _ = run(tmp_path, [table()], [rec()], monkeypatch)
    it = PageEditor(pp).get("p0003-b001")
    assert out[0]["applied"] and it["rows"] == [["a", "b"], ["1", "2"]]
    assert "image_fallback_reason" not in it and it["table_fallback"]["verdict"] == "accepted"


def test_a_rejected_read_is_still_written_and_named_in_a_note(tmp_path, monkeypatch):
    r = rec("rejected", 0.86)
    r["check"]["missing"] = ["gcagcg"]
    pp, out, _ = run(tmp_path, [table()], [r], monkeypatch)
    assert out[0]["applied"] and "rejected" in out[0]["note"] and "gcagcg" in out[0]["note"]
    assert PageEditor(pp).get("p0003-b001")["rows"]


def test_a_read_under_the_floor_keeps_the_image(tmp_path, monkeypatch):
    pp, out, _ = run(tmp_path, [table()], [rec("rejected", 0.03, rows=[["Header"]])], monkeypatch)
    it = PageEditor(pp).get("p0003-b001")
    assert not out[0]["applied"] and not it["rows"] and it["table_fallback"]["kept_image"]


def test_an_unchecked_read_is_written(tmp_path, monkeypatch):
    r = rec("unchecked")
    r["check"].update(checked=False, coverage=None, precision=None)
    _, out, _ = run(tmp_path, [table()], [r], monkeypatch)
    assert out[0]["applied"]


def test_an_api_failure_leaves_the_image_and_is_not_retried(tmp_path, monkeypatch):
    sd, pp = plan(tmp_path, [table()])
    pp_items = lambda: PageEditor(pp).get("p0003-b001")      # noqa: E731
    monkeypatch.setattr(tablefill, "transcribe_table",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    async def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(tablefill, "transcribe_table", boom)
    cfg = SimpleNamespace(vision_model="m", transcribe_dpi=400, table_min_coverage=0.5)
    out = asyncio.run(tablefill.fill_image_tables([sd], cfg, None, None))
    assert not out[0]["applied"] and "boom" in out[0]["why"]
    assert not pp_items()["rows"] and tablefill.pending([sd]) == []


def test_a_second_pass_does_not_pay_again(tmp_path, monkeypatch):
    sd, pp = plan(tmp_path, [table()])

    async def fake(*a, **k):
        return rec()
    monkeypatch.setattr(tablefill, "transcribe_table", fake)
    cfg = SimpleNamespace(vision_model="m", transcribe_dpi=400, table_min_coverage=0.5)
    asyncio.run(tablefill.fill_image_tables([sd], cfg, None, None))
    assert tablefill.pending([sd]) == []
    assert asyncio.run(tablefill.fill_image_tables([sd], cfg, None, None)) == []


def test_a_page_the_editor_refuses_keeps_its_image_without_crashing(tmp_path, monkeypatch):
    # duplicate item ids: validate() refuses to save such a page
    sd, pp = plan(tmp_path, [table(), {"id": "p0003-b001", "kind": "text", "markdown": "x"}])

    async def fake(*a, **k):
        return rec()
    monkeypatch.setattr(tablefill, "transcribe_table", fake)
    cfg = SimpleNamespace(vision_model="m", transcribe_dpi=400, table_min_coverage=0.5)
    out = asyncio.run(tablefill.fill_image_tables([sd], cfg, None, None))
    assert not out[0]["applied"] and "refused" in out[0]["why"]


def test_tables_that_already_have_rows_and_figures_are_untouched(tmp_path):
    sd, _ = plan(tmp_path, [table("a", rows=[["x", "y"]]),
                            {"id": "f", "kind": "figure", "bbox": [0, 0, 5, 5]}])
    assert tablefill.pending([sd]) == []
