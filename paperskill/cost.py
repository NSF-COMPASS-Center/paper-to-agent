"""Token and cost accounting for one conversion.

Every model call in this system goes through `Ledger.record`, so the per-run
total is complete by construction rather than by remembering to instrument.

Writes two files into the run directory:
  cost.jsonl  one line per API call, append-only
  cost.json   rolled-up summary, rewritten on each flush
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class CallCost:
    stage: str                  # "bundle" | "page" | "adjudicate" | "verify_review"
    unit: str                   # page number, source id, or "-"
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    usd: float
    seconds: float
    ts: float = field(default_factory=time.time)
    note: str = ""

    @property
    def uncached_input(self) -> int:
        return max(0, self.input_tokens - self.cached_tokens)


def extract_usage(obj: Any) -> tuple[int, int, int, int]:
    """Pull (input, cached, output, reasoning) out of whatever usage shape we got.

    Handles the Responses API (`input_tokens` / `input_tokens_details.cached_tokens`),
    the Agents SDK `Usage`, and chat-completions style (`prompt_tokens`).
    Unknown shapes return zeros rather than raising -- a cost number is never
    worth failing a conversion over, and a zero row is visible in the report.
    """
    if obj is None:
        return 0, 0, 0, 0
    g = lambda *names: next((int(v) for n in names
                             if (v := getattr(obj, n, None)) is not None), 0)
    inp = g("input_tokens", "prompt_tokens")
    out = g("output_tokens", "completion_tokens")
    cached = 0
    det = getattr(obj, "input_tokens_details", None) or getattr(obj, "prompt_tokens_details", None)
    if det is not None:
        cached = int(getattr(det, "cached_tokens", 0) or 0)
    reasoning = 0
    odet = getattr(obj, "output_tokens_details", None) or getattr(obj, "completion_tokens_details", None)
    if odet is not None:
        reasoning = int(getattr(odet, "reasoning_tokens", 0) or 0)
    return inp, cached, out, reasoning


class Ledger:
    def __init__(self, run_dir: Path, pricing: dict[str, dict[str, float]]):
        # Deliberately does NOT create run_dir. It lives inside the review
        # directory, and `paper_bundle.py prepare` refuses to run if that
        # directory already exists -- so creation is deferred to the first write.
        self.run_dir = Path(run_dir)
        self.pricing = pricing
        self.calls: list[CallCost] = []
        self.unpriced: set[str] = set()
        self.subcommands: list[dict] = []   # deterministic steps: free, but timed
        self.t0 = time.time()
        self._jsonl = self.run_dir / "cost.jsonl"

    def _ensure(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # --- recording ----------------------------------------------------------
    def price(self, model: str, inp: int, cached: int, out: int) -> float:
        rates = self.pricing.get(model)
        if rates is None:
            # try a prefix match so "gpt-5-2026-01-01" still prices as gpt-5
            for k in sorted(self.pricing, key=len, reverse=True):
                if model.startswith(k):
                    rates = self.pricing[k]
                    break
        if rates is None:
            self.unpriced.add(model)
            return 0.0
        uncached = max(0, inp - cached)
        return (uncached * rates["input"]
                + cached * rates.get("cached_input", rates["input"])
                + out * rates["output"]) / 1_000_000

    def record(self, *, stage: str, unit: str, model: str, usage: Any,
               seconds: float = 0.0, note: str = "") -> CallCost:
        inp, cached, out, reasoning = extract_usage(usage)
        c = CallCost(stage=stage, unit=str(unit), model=model,
                     input_tokens=inp, cached_tokens=cached, output_tokens=out,
                     reasoning_tokens=reasoning,
                     usd=self.price(model, inp, cached, out),
                     seconds=seconds, note=note)
        self.calls.append(c)
        self._ensure()
        with self._jsonl.open("a") as fh:
            fh.write(json.dumps(c.__dict__) + "\n")
        return c

    def record_run_result(self, *, stage: str, unit: str, result: Any,
                          seconds: float = 0.0, note: str = "") -> list[CallCost]:
        """Record every model response inside one Agents-SDK `Runner.run` result.

        Per-response rather than the aggregate so a runaway page shows which turn
        ran away. Falls back to the accumulated context usage if the per-response
        list is not exposed by this SDK version.
        """
        out: list[CallCost] = []
        responses: Iterable = getattr(result, "raw_responses", None) or []
        for i, resp in enumerate(responses):
            model = (getattr(resp, "model", None)
                     or getattr(getattr(result, "last_agent", None), "model", None) or "unknown")
            out.append(self.record(stage=stage, unit=unit, model=str(model),
                                   usage=getattr(resp, "usage", None),
                                   seconds=seconds if i == 0 else 0.0,
                                   note=f"{note} turn{i+1}".strip()))
        if not out:
            ctx = getattr(result, "context_wrapper", None)
            out.append(self.record(stage=stage, unit=unit, model="unknown",
                                   usage=getattr(ctx, "usage", None),
                                   seconds=seconds, note=f"{note} aggregate".strip()))
        return out

    def record_subcommand(self, name: str, seconds: float, ok: bool) -> None:
        # in-memory only; written out by flush(), after prepare has made the dir
        self.subcommands.append({"name": name, "seconds": round(seconds, 2), "ok": ok})

    # --- reporting ----------------------------------------------------------
    def _group(self, key) -> dict[str, dict]:
        g: dict[str, dict] = {}
        for c in self.calls:
            d = g.setdefault(key(c), {"calls": 0, "input": 0, "cached": 0,
                                      "output": 0, "reasoning": 0, "usd": 0.0})
            d["calls"] += 1
            d["input"] += c.input_tokens
            d["cached"] += c.cached_tokens
            d["output"] += c.output_tokens
            d["reasoning"] += c.reasoning_tokens
            d["usd"] += c.usd
        return g

    def summary(self) -> dict:
        tot = {"calls": len(self.calls),
               "input_tokens": sum(c.input_tokens for c in self.calls),
               "cached_tokens": sum(c.cached_tokens for c in self.calls),
               "output_tokens": sum(c.output_tokens for c in self.calls),
               "reasoning_tokens": sum(c.reasoning_tokens for c in self.calls),
               "usd": sum(c.usd for c in self.calls),
               "wall_seconds": round(time.time() - self.t0, 1)}
        cache_hit = (tot["cached_tokens"] / tot["input_tokens"]) if tot["input_tokens"] else 0.0
        return {"total": tot,
                "cumulative_all_runs": self.cumulative(),
                "cache_hit_rate": cache_hit,
                "by_stage": self._group(lambda c: c.stage),
                "by_model": self._group(lambda c: c.model),
                "by_page": self._group(lambda c: c.unit) if any(c.stage == "page" for c in self.calls) else {},
                "subcommands": self.subcommands,
                "unpriced_models": sorted(self.unpriced),
                "pricing_used": self.pricing}

    @staticmethod
    def _round(obj):
        """Round floats for serialization only. summary() keeps full precision so the
        total always reconciles against cost.jsonl; 6 dp is a millionth of a dollar."""
        if isinstance(obj, float):
            return round(obj, 6)
        if isinstance(obj, dict):
            return {k: Ledger._round(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [Ledger._round(v) for v in obj]
        return obj

    def cumulative(self) -> dict:
        """Totals over every call ever logged for this review directory.

        `cost.json` is rewritten each run, so after a `--resume` it reported only
        that run's single call while cost.jsonl held hundreds. Both numbers are
        useful -- the run figure for "what did this cost", the cumulative for
        "what has this paper cost me so far" -- so report both.
        """
        tot = {"calls": 0, "input_tokens": 0, "cached_tokens": 0,
               "output_tokens": 0, "usd": 0.0}
        if not self._jsonl.exists():
            return tot
        try:
            for line in self._jsonl.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                tot["calls"] += 1
                tot["input_tokens"] += int(row.get("input_tokens") or 0)
                tot["cached_tokens"] += int(row.get("cached_tokens") or 0)
                tot["output_tokens"] += int(row.get("output_tokens") or 0)
                tot["usd"] += float(row.get("usd") or 0.0)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return tot

    def flush(self) -> Path:
        self._ensure()
        p = self.run_dir / "cost.json"
        p.write_text(json.dumps(self._round(self.summary()), indent=2))
        return p

    def report(self) -> str:
        s = self.summary()
        t = s["total"]
        L = []
        L.append("")
        L.append("=" * 68)
        L.append(f"  COST  ${t['usd']:.4f}   {t['calls']} calls   {t['wall_seconds']:.0f}s wall")
        L.append("=" * 68)
        L.append(f"  input {t['input_tokens']:>9,}  (cached {t['cached_tokens']:,} "
                 f"= {s['cache_hit_rate']*100:.0f}%)")
        L.append(f"  output{t['output_tokens']:>9,}  (reasoning {t['reasoning_tokens']:,})")
        L.append("")
        L.append(f"  {'stage':<16}{'calls':>6}{'input':>10}{'output':>9}{'usd':>9}")
        L.append("  " + "-" * 50)
        for k, d in sorted(s["by_stage"].items(), key=lambda kv: -kv[1]["usd"]):
            L.append(f"  {k:<16}{d['calls']:>6}{d['input']:>10,}{d['output']:>9,}{d['usd']:>9.4f}")
        if s["by_model"]:
            L.append("")
            L.append(f"  {'model':<16}{'calls':>6}{'input':>10}{'output':>9}{'usd':>9}")
            L.append("  " + "-" * 50)
            for k, d in sorted(s["by_model"].items(), key=lambda kv: -kv[1]["usd"]):
                L.append(f"  {k:<16}{d['calls']:>6}{d['input']:>10,}{d['output']:>9,}{d['usd']:>9.4f}")
        if s["by_page"]:
            worst = sorted(s["by_page"].items(), key=lambda kv: -kv[1]["usd"])[:5]
            L.append("")
            L.append("  most expensive pages: " +
                     ", ".join(f"p{k} ${d['usd']:.3f}" for k, d in worst))
        if s["unpriced_models"]:
            L.append("")
            L.append(f"  !! NO PRICING for {', '.join(s['unpriced_models'])} "
                     f"-- those calls counted as $0. Add them to [pricing] in your config.")
        cum = s.get("cumulative_all_runs") or {}
        if cum.get("calls", 0) > t["calls"]:
            L.append("")
            L.append(f"  this review directory, all runs: ${cum['usd']:.4f} "
                     f"over {cum['calls']} calls")
        L.append("=" * 68)
        return "\n".join(L)
