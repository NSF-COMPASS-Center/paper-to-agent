"""Per-paper quality metrics and corpus triage.

With ~150 papers nobody reads every package. Each run records a handful of counts that
correlate with the defects found so far, and `summarize` ranks papers by them so human
attention goes to the outliers. All counts come from artifacts the run already wrote;
nothing here calls a model.
"""
from __future__ import annotations

import json
from pathlib import Path


def table_image_rate(plan_dirs: list[Path]) -> tuple[int, int]:
    """(tables kept only as an image, tables). A table with no rows is a JPEG."""
    imgs = total = 0
    for sd in plan_dirs:
        for pp in (sd / "pages").glob("page-*.json"):
            try:
                items = json.loads(pp.read_text()).get("items", [])
            except (OSError, json.JSONDecodeError):
                continue
            for it in items:
                if it.get("kind") == "table":
                    total += 1
                    imgs += 0 if it.get("rows") else 1
    return imgs, total


def paper_metrics(report: dict, plan_dirs: list[Path]) -> dict:
    st = report.get("stages") or {}
    final = report.get("final") or {}
    crops = st.get("crop_repairs") or []
    floats = st.get("float_hoists") or []
    acc = st.get("word_accounting") or []
    imgs, tables = table_image_rate(plan_dirs)
    tf = st.get("table_fallback") or []
    cost = ((report.get("cost") or {}).get("cumulative_all_runs") or {}).get("usd")
    return {
        "status": final.get("status"),
        "draft": final.get("status") == "delivered_draft",
        "limitations": len(final.get("limitations") or []),
        "crops_grown": sum(1 for c in crops if c.get("applied")),
        "crops_unresolved": sum(1 for c in crops if not c.get("applied")),
        "floats_moved": sum(1 for h in floats if h.get("applied")),
        "floats_left": sum(1 for h in floats if not h.get("applied")),
        "pages_with_lost_words": len(acc),
        "lost_words": sum(sum((a.get("causes") or {}).values()) for a in acc),
        "tables": tables,
        "tables_as_images": imgs,
        "tables_filled": sum(1 for t in tf if t.get("applied")),
        "tables_not_accepted": sum(1 for t in tf if t.get("applied")
                                   and t.get("verdict") != "accepted"),
        "delivery_fails": final.get("delivery_fails"),
        "delivery_warns": final.get("delivery_warns"),
        "cost_usd": cost,
    }


# Reasons to look at a paper, in the order they are listed.
def flags(m: dict) -> list[str]:
    out = []
    if m.get("draft"):
        out.append("draft")
    if m.get("crops_unresolved"):
        out.append("crop unresolved")
    if (m.get("lost_words") or 0) >= 10:
        out.append("lost words")
    if m.get("tables") and (m.get("tables_as_images") or 0) / m["tables"] > 0.5:
        out.append("tables as images")
    if m.get("floats_left"):
        out.append("float mid-sentence")
    if m.get("delivery_fails"):
        out.append("delivery fail")
    return out


def load_reports(dirs: list[Path]) -> list[tuple[str, dict]]:
    rows = []
    for d in dirs:
        p = d / "agent-run" / "report.json"
        if p.exists():
            try:
                rep = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            m = rep.get("metrics")
            if m is None:       # runs from before metrics existed
                m = paper_metrics(rep, [x for x in (d / "documents").glob("*") if x.is_dir()])
            rows.append((rep.get("name") or d.name, m))
    return rows


def render_table(rows: list[tuple[str, dict]]) -> str:
    head = f"{'paper':<14}{'status':<17}{'lost':>5}{'crop!':>6}{'tbl img':>9}{'flt!':>5}{'$':>8}  flags"
    lines = [head, "-" * len(head)]
    for name, m in sorted(rows, key=lambda r: (-len(flags(r[1])), r[0])):
        tbl = f"{m['tables_as_images']}/{m['tables']}" if m.get("tables") else "-"
        cost = f"{m['cost_usd']:.2f}" if isinstance(m.get("cost_usd"), (int, float)) else "-"
        lines.append(f"{name:<14}{str(m.get('status')):<17}{m.get('lost_words', 0):>5}"
                     f"{m.get('crops_unresolved', 0):>6}{tbl:>9}{m.get('floats_left', 0):>5}"
                     f"{cost:>8}  {', '.join(flags(m))}")
    return "\n".join(lines)
