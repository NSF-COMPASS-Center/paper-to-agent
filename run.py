#!/usr/bin/env python3
"""Paper -> reviewed skill package, through the OpenAI Agents SDK.

  export OPENAI_API_KEY=...
  python run.py convert paper.pdf --name example-paper --out dist/example-paper

  python run.py convert main.pdf supp.pdf tables.xlsx \
      --name example-paper --title 'Full title' --main main.pdf \
      --work runs/example-review --out dist/example-paper \
      --asset-dpi 600 --concurrency 4

  python run.py convert paper.pdf --name x --out dist/x --pages 3,4,7   # subset
  python run.py convert paper.pdf --name x --out dist/x --resume        # keep extraction
  python run.py cost runs/example-review                                # re-print the report
  python run.py pricing                                                 # show the price table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from paperskill.config import Config
from paperskill.coordinator import convert
from paperskill.cost import Ledger


def parse_pages(s: str | None) -> list[int] | None:
    if not s:
        return None
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def cmd_convert(args) -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY first.", file=sys.stderr)
        return 1
    cfg = Config.load(args.config)
    for attr, val in (("review_model", args.model), ("vision_model", args.vision_model),
                      ("asset_dpi", args.asset_dpi), ("preview_dpi", args.preview_dpi),
                      ("max_image_side", args.max_image_side),
                      ("max_turns_per_page", args.max_turns_per_page)):
        if val:
            setattr(cfg, attr, val)
    if args.notes_in_package:
        cfg.notes_in_package = True
    if args.no_table_fallback:
        cfg.table_fallback = False
    if args.draft:
        cfg.require_reviewed = False
    if args.pages and not args.draft:
        print("note: --pages leaves the rest of the document unreviewed, so the build will "
              "refuse. Add --draft to build a draft package from the subset.", file=sys.stderr)

    inputs = [Path(p).resolve() for p in args.inputs]
    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        print(f"Input not found: {', '.join(missing)}", file=sys.stderr)
        return 1

    work = Path(args.work or f"runs/{args.name}-review").resolve()
    out = Path(args.out).resolve()

    report = asyncio.run(convert(
        inputs, args.name, work, out, cfg,
        title=args.title, main=Path(args.main).resolve() if args.main else None,
        attachments=[Path(a).resolve() for a in args.attachment],
        pages=parse_pages(args.pages), concurrency=args.concurrency,
        force=args.force, resume=args.resume,
        reuse_from=Path(args.reuse_from).resolve() if args.reuse_from else None,
        ocr_pages=parse_pages(args.ocr_pages),
    ))

    if report.get("error"):
        print(f"\nFAILED : {report['error']}", file=sys.stderr)
        return 1

    final = report.get("final", {})
    status = final.get("status")
    print(f"\nstatus : {status}")
    print(f"package: {final.get('output')}")
    print(f"review : {work}")
    print(f"cost   : {work / 'agent-run' / 'cost.json'}")
    if final.get("note"):
        print(f"note   : {final['note']}")

    # Exit code answers "did this do what was asked", not "is this a deliverable".
    # A --draft run that produced a draft succeeded; it just is not publishable.
    if status in {"reviewed", "reviewed_with_limitations"}:
        return 0
    if status == "delivered_draft":
        print(f"limits : {final.get('limitations_file')}")
        print("\nDelivered as a DRAFT: strict verification did not pass. The limitations "
              "are listed in the file above and inside the package notes.")
        return 3
    if status == "draft" and args.draft:
        print("\nDraft build succeeded. Not a deliverable -- rerun without --pages/--draft "
              "for the full package.")
        return 0
    for err in (final.get("build_error"), final.get("verify_error")):
        if err:
            print(f"\nerror  : {err}", file=sys.stderr)
    return 2


def cmd_cost(args) -> int:
    run_dir = Path(args.work)
    run_dir = run_dir if run_dir.name == "agent-run" else run_dir / "agent-run"
    summary = run_dir / "cost.json"
    if not summary.exists():
        print(f"No cost.json under {run_dir}", file=sys.stderr)
        return 1
    data = json.loads(summary.read_text())
    led = Ledger(run_dir, data.get("pricing_used") or {})
    # rebuild the in-memory rows from the append-only log so the report is exact
    jsonl = run_dir / "cost.jsonl"
    if jsonl.exists():
        from paperskill.cost import CallCost
        for line in jsonl.read_text().splitlines():
            if line.strip():
                led.calls.append(CallCost(**json.loads(line)))
        led.subcommands = data.get("subcommands", [])
        led.t0 = led.t0 - float(data.get("total", {}).get("wall_seconds", 0) or 0)
    print(led.report())
    return 0


def cmd_summarize(args) -> int:
    from paperskill.metrics import load_reports, render_table
    dirs = [Path(d) for d in args.dirs]
    rows = load_reports(dirs)
    if not rows:
        print("No agent-run/report.json found under those directories.", file=sys.stderr)
        return 1
    print(render_table(rows))
    return 0


def cmd_pricing(args) -> int:
    cfg = Config.load(args.config)
    print(f"{'model':<18}{'input':>12}{'cached':>12}{'output':>12}   (USD per 1M tokens)")
    print("-" * 68)
    for m, r in sorted(cfg.pricing.items()):
        print(f"{m:<18}{r['input']:>12.3f}{r.get('cached_input', r['input']):>12.3f}"
              f"{r['output']:>12.3f}")
    print("\nVerify against current OpenAI pricing; override with [pricing] in a "
          "--config TOML.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="Run a full conversion")
    c.add_argument("inputs", nargs="+", help="PDFs, spreadsheets, figures")
    c.add_argument("--name", required=True, help="Package identifier, e.g. example-paper")
    c.add_argument("--out", required=True, help="Delivery directory for the package")
    c.add_argument("--work", help="Review directory (default runs/<name>-review)")
    c.add_argument("--title", help="Full paper title")
    c.add_argument("--main", help="Explicit main PDF when several are supplied")
    c.add_argument("--attachment", action="append", default=[],
                   help="Keep this file as opaque bytes instead of parsing it")
    c.add_argument("--pages", help="Review only these pages, e.g. 3,4,7-9 (for debugging)")
    c.add_argument("--ocr-pages", help="Replace these pages' text layer with OCR, e.g. 7,12 "
                   "(for pages whose native text is garbled; applies to the main PDF)")
    c.add_argument("--concurrency", type=int, default=3, help="Pages reviewed in parallel")
    c.add_argument("--resume", action="store_true", help="Keep existing prepare/extract output")
    c.add_argument("--force", action="store_true", help="Re-review pages already marked reviewed")
    c.add_argument("--draft", action="store_true",
                   help="Build even though some pages are unreviewed. Use with --pages to "
                        "exercise the build and verify path on a cheap subset; the package "
                        "is labelled a draft and strict verification still reports unreviewed.")
    c.add_argument("--reuse-from", help="Reuse review decisions from a byte-identical review dir")
    c.add_argument("--model", help="Review model (default gpt-5)")
    c.add_argument("--vision-model", help="Model for high-resolution transcription")
    c.add_argument("--asset-dpi", type=int, help="Figure/table crop DPI, 72-600 (default 600)")
    c.add_argument("--max-turns-per-page", type=int,
                   help="Turn cap for one page's review (default 28). Raise it if pages come "
                        "back max_turns.")
    c.add_argument("--max-image-side", type=int,
                   help="Longest side of a crop in px, 720-6000 (default 4000). The engine "
                        "clamps to this AFTER rendering, so a low value cancels a high --asset-dpi.")
    c.add_argument("--preview-dpi", type=int, help="Page preview DPI, 72-600 (default 200)")
    c.add_argument("--notes-in-package", action="store_true",
                   help="Print the conversion notes at the end of paper.md (default: write "
                        "them to <out>.NOTES.md beside the package instead)")
    c.add_argument("--no-table-fallback", action="store_true",
                   help="Leave tables the reviewer kept as images; skip the deterministic "
                        "transcription pass (paperskill.tablefill)")
    c.add_argument("--config", help="TOML config overriding models, DPI and pricing")
    c.set_defaults(fn=cmd_convert)

    c = sub.add_parser("cost", help="Re-print the cost report for a run")
    c.add_argument("work", help="Review directory, or its agent-run/ subdirectory")
    c.set_defaults(fn=cmd_cost)

    c = sub.add_parser("summarize", help="Rank finished runs by quality metrics")
    c.add_argument("dirs", nargs="+", help="Review directories, e.g. runs/*-review")
    c.set_defaults(fn=cmd_summarize)

    c = sub.add_parser("pricing", help="Show the active price table")
    c.add_argument("--config")
    c.set_defaults(fn=cmd_pricing)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
