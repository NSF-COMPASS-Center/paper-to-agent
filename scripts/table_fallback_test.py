#!/usr/bin/env python3
"""Run the table fallback (paperskill.tabletx) on tables the page reviewer left as images.

Read-only on every run directory. Output goes to table_tests/<case>/ :
  crop.png      what the model saw
  table.csv     the transcription
  table.md      the same as markdown
  result.json   verdict, PDF-text check (coverage/precision/missing/invented), attempts, cost
and table_tests/INDEX.md summarises all cases.

  python scripts/table_fallback_test.py            # every known image table
  python scripts/table_fallback_test.py paper-3    # cases whose name contains this
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openai import AsyncOpenAI  # noqa: E402

from paperskill.config import Config  # noqa: E402
from paperskill.cost import Ledger  # noqa: E402
from paperskill.tabletx import to_csv, to_markdown, transcribe_table  # noqa: E402

OUT = ROOT / "table_tests"

# (run dir, page, item id): the image tables of the current deliveries, minus paper 57.
CASES = [
    ("paper-15-review-v3", 3, "p0003-b011-s6"),
    ("paper-3-review", 19, "p0019-b015"),
    ("paper-3-review", 20, "p0020-b001"),
    ("paper-3-review", 24, "p0024-b004"),
    ("paper-3-review", 24, "p0024-b006"),
    ("paper-8-review", 4, "p0004-b012"),
    ("paper-53-review", 9, "p0009-b006"),
    ("ls-elazharybovineadenovirus1979-review", 6, "p0006-b001"),
    ("ls-songerrhairborneviruses1967-review", 7, "p0007-b003"),
    ("uv-braunstein-1996-review", 6, "p0006-b003"),
]


def find(run: str, page: int, item_id: str) -> tuple[Path, dict]:
    docs = ROOT / "runs" / run / "documents"
    for sd in sorted(docs.iterdir()):
        plan = sd / "pages" / f"page-{page:04d}.json"
        if plan.exists() and (sd / "source.pdf").exists():
            data = json.loads(plan.read_text())
            for it in data["items"]:
                if it["id"] == item_id:
                    return sd / "source.pdf", it
    raise SystemExit(f"{run} p{page} {item_id}: not found")


async def run_case(client, cfg, ledger, run: str, page: int, item_id: str) -> dict:
    pdf, item = find(run, page, item_id)
    name = f"{run.removesuffix('-review')}-p{page}-{item_id}"
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    before = len(ledger.calls)
    rec = await transcribe_table(client, cfg.vision_model, pdf, page, item["bbox"],
                                 cfg.transcribe_dpi, ledger, unit=name)
    png = rec.pop("png")
    (d / "crop.png").write_bytes(png)
    (d / "table.csv").write_text(to_csv(rec["rows"]))
    (d / "table.md").write_text(to_markdown(rec["rows"]))
    rec["usd"] = round(sum(c.usd for c in ledger.calls[before:]), 4)
    rec["reviewer_reason"] = item.get("image_fallback_reason")
    rec["case"] = name
    (d / "result.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    return rec


def index(recs: list[dict]) -> str:
    lines = ["# Table fallback test", "",
             "| case | verdict | rows x cols | coverage | precision | cut off | bands | uncertain | $ |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in recs:
        c = r["check"]
        cols = len(r["rows"][0]) if r["rows"] else 0
        lines.append(f"| [{r['case']}]({r['case']}/table.md) | {r['verdict']} | "
                     f"{len(r['rows'])} x {cols} | {c['coverage']} | {c['precision']} | "
                     f"{','.join(r['cut_off']) or '-'} | {r['bands'] or '-'} | {len(r['uncertain'])} | {r['usd']} |")
    lines.append("")
    return "\n".join(lines)


async def main(argv: list[str]) -> int:
    cfg = Config.load(None)
    OUT.mkdir(exist_ok=True)
    ledger = Ledger(OUT / "_cost", cfg.pricing)
    client = AsyncOpenAI()
    todo = [c for c in CASES if not argv or any(a in c[0] for a in argv)]
    recs = await asyncio.gather(*(run_case(client, cfg, ledger, *c) for c in todo))
    (OUT / "INDEX.md").write_text(index(list(recs)))
    print(index(list(recs)))
    print(f"total ${sum(r['usd'] for r in recs):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
