"""A readable index of finished packages: which one is current for each paper.

`dist/<name>/` is always the latest build of that paper. When a paper is rebuilt, the
package it replaces moves to `dist/_archive/<name>/<UTC timestamp>/`, so nothing is
overwritten and nothing is named `.previous-2`. `dist/INDEX.md` lists every paper with
its status, build time, limitations and cost, generated from `dist/INDEX.json`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

INDEX_JSON, INDEX_MD = "INDEX.json", "INDEX.md"


def archive_path(output: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base = output.parent / "_archive" / output.name / stamp
    n, path = 1, base
    while path.exists():
        n += 1
        path = base.parent / f"{stamp}-{n}"
    return path


def record_from_report(report: dict, source: str = "", built_ts: float | None = None) -> dict:
    final = report.get("final") or {}
    m = report.get("metrics") or {}
    return {"name": report.get("name"), "source": source,
            "status": final.get("status"),
            "built_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(built_ts)),
            "package": final.get("output"), "work": report.get("work"),
            "limitations": len(final.get("limitations") or []),
            "limitations_file": final.get("limitations_file"),
            "tables": m.get("tables"), "tables_as_images": m.get("tables_as_images"),
            "cost_usd": m.get("cost_usd"),
            "model": (report.get("config") or {}).get("review_model")}


def update_index(dist: Path, record: dict) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    jp = dist / INDEX_JSON
    try:
        rows = json.loads(jp.read_text())
    except (OSError, json.JSONDecodeError):
        rows = []
    rows = [r for r in rows if r.get("name") != record.get("name")] + [record]
    rows.sort(key=lambda r: str(r.get("name")))
    jp.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    (dist / INDEX_MD).write_text(render_markdown(rows))
    return dist / INDEX_MD


def render_markdown(rows: list[dict]) -> str:
    lines = ["# Converted papers: latest build of each", "",
             "`<name>/` in this folder is always the current build. Older builds are in "
             "`_archive/<name>/<timestamp>/`.", "",
             "| paper | status | built | tables as images | limitations | cost | package | original |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        tbl = (f"{r.get('tables_as_images')}/{r.get('tables')}"
               if r.get("tables") else "-")
        cost = f"${r['cost_usd']:.2f}" if isinstance(r.get("cost_usd"), (int, float)) else "-"
        pkg = Path(r["package"]).name if r.get("package") else "-"
        lim = str(r.get("limitations") or 0)
        if r.get("limitations_file"):
            lim += f" ([list]({Path(r['limitations_file']).name}))"
        src = r.get("source")
        orig = f"[pdf](_sources/{Path(src).name})" if src else "-"
        lines.append(f"| {r.get('name')} | {r.get('status')} | {r.get('built_at')} | {tbl} "
                     f"| {lim} | {cost} | {pkg} | {orig} |")
    return "\n".join(lines) + "\n"
