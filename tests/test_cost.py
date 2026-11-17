"""Cost accounting. No API key needed."""
import json
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paperskill.cost import Ledger, extract_usage  # noqa: E402

PRICING = {"gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00}}


def usage(inp, cached, out, reasoning=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning))


def test_extract_usage_responses_shape():
    assert extract_usage(usage(100, 40, 20, 5)) == (100, 40, 20, 5)


def test_extract_usage_chat_shape():
    u = SimpleNamespace(prompt_tokens=10, completion_tokens=3)
    assert extract_usage(u) == (10, 0, 3, 0)


def test_extract_usage_none_is_zeros():
    assert extract_usage(None) == (0, 0, 0, 0)


def test_cached_tokens_are_billed_at_the_cached_rate(tmp_path):
    led = Ledger(tmp_path, PRICING)
    # 1,000,000 input of which 1,000,000 cached, no output -> the cached rate only
    led.record(stage="page", unit="1", model="gpt-5", usage=usage(1_000_000, 1_000_000, 0))
    assert abs(led.summary()["total"]["usd"] - 0.125) < 1e-9
    # all-uncached costs the full input rate
    led2 = Ledger(tmp_path / "b", PRICING)
    led2.record(stage="page", unit="1", model="gpt-5", usage=usage(1_000_000, 0, 0))
    assert abs(led2.summary()["total"]["usd"] - 1.25) < 1e-9


def test_mixed_call_is_split_correctly(tmp_path):
    led = Ledger(tmp_path, PRICING)
    led.record(stage="page", unit="1", model="gpt-5", usage=usage(1_000_000, 400_000, 100_000))
    expected = 0.6 * 1.25 + 0.4 * 0.125 + 0.1 * 10.00
    assert abs(led.summary()["total"]["usd"] - expected) < 1e-9


def test_dated_model_prices_by_prefix(tmp_path):
    led = Ledger(tmp_path, PRICING)
    led.record(stage="page", unit="1", model="gpt-5-2026-03-01", usage=usage(1_000_000, 0, 0))
    assert abs(led.summary()["total"]["usd"] - 1.25) < 1e-9
    assert led.unpriced == set()


def test_unknown_model_is_flagged_not_silently_free(tmp_path):
    led = Ledger(tmp_path, PRICING)
    led.record(stage="page", unit="1", model="o9-ultra", usage=usage(500, 0, 50))
    s = led.summary()
    assert s["total"]["usd"] == 0.0
    assert s["unpriced_models"] == ["o9-ultra"]
    assert "NO PRICING" in led.report()


def test_jsonl_has_one_line_per_call_and_summary_matches(tmp_path):
    led = Ledger(tmp_path, PRICING)
    for p in range(1, 4):
        led.record(stage="page", unit=str(p), model="gpt-5", usage=usage(1000, 100, 50))
    led.record(stage="bundle", unit="-", model="gpt-5", usage=usage(5000, 0, 200))
    lines = [json.loads(l) for l in (tmp_path / "cost.jsonl").read_text().splitlines() if l.strip()]
    assert len(lines) == 4
    s = led.summary()
    assert s["total"]["calls"] == 4
    assert s["by_stage"]["page"]["calls"] == 3
    assert s["by_stage"]["bundle"]["input"] == 5000
    assert set(s["by_page"]) == {"1", "2", "3", "-"}
    assert abs(s["total"]["usd"] - sum(l["usd"] for l in lines)) < 1e-9
    assert json.loads(led.flush().read_text())["total"]["calls"] == 4


def test_run_result_records_each_turn(tmp_path):
    led = Ledger(tmp_path, PRICING)
    result = SimpleNamespace(raw_responses=[
        SimpleNamespace(model="gpt-5", usage=usage(2000, 0, 100)),
        SimpleNamespace(model="gpt-5", usage=usage(3000, 1800, 150)),
    ])
    rows = led.record_run_result(stage="page", unit="7", result=result, seconds=4.2)
    assert len(rows) == 2
    assert led.summary()["total"]["input_tokens"] == 5000
    assert led.summary()["cache_hit_rate"] == 0.36


def test_run_result_falls_back_to_aggregate_usage(tmp_path):
    led = Ledger(tmp_path, PRICING)
    result = SimpleNamespace(raw_responses=[],
                             context_wrapper=SimpleNamespace(usage=usage(900, 0, 40)))
    rows = led.record_run_result(stage="page", unit="2", result=result)
    assert len(rows) == 1 and rows[0].input_tokens == 900


# --- exit 2 is a state, not a failure ---------------------------------------
# verify --strict exits 2 for a pending verification state and still prints its
# JSON status. Treating that as a failure surfaced an unrelated fontTools stderr
# warning as "error:" and sent debugging in the wrong direction.

def test_pending_verify_state_is_not_an_error():
    from paperskill.bundle import CmdResult
    r = CmdResult(False, 2, {"status": "unresolved_discrepancies"},
                  '{"status":"unresolved_discrepancies"}',
                  "fontTools is required to fully parse ...\n", 1.0)
    assert r.status == "unresolved_discrepancies"
    assert r.error == ""                      # ran fine, reported a pending state
    assert "fontTools" in r.warnings          # the stderr is a warning, not a failure


def test_hard_failure_still_reports_its_message():
    from paperskill.bundle import CmdResult
    r = CmdResult(False, 1, {}, "", "Error: Use a new output directory\n", 1.0)
    assert r.status == ""
    assert r.error == "Error: Use a new output directory"
    assert r.warnings == ""


def test_success_has_no_error_and_keeps_warnings():
    from paperskill.bundle import CmdResult
    r = CmdResult(True, 0, {"status": "reviewed"}, "{}", "some notice\n", 1.0)
    assert r.error == "" and r.warnings == "some notice"


def test_cumulative_spans_runs_while_total_is_this_run(tmp_path):
    """cost.json is rewritten each run, so after --resume it reported one call
    while cost.jsonl held hundreds. Both figures are now reported."""
    led = Ledger(tmp_path, PRICING)
    for _ in range(3):
        led.record(stage="page", unit="1", model="gpt-5", usage=usage(1000, 0, 100))
    led.flush()
    first_total = led.summary()["total"]["usd"]

    # a second "run" against the same review directory
    led2 = Ledger(tmp_path, PRICING)
    led2.record(stage="bundle", unit="-", model="gpt-5", usage=usage(900, 0, 50))
    s = led2.summary()
    assert s["total"]["calls"] == 1                       # this run
    assert s["cumulative_all_runs"]["calls"] == 4         # everything logged
    assert s["cumulative_all_runs"]["usd"] > first_total
    assert "all runs" in led2.report()
