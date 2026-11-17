#!/usr/bin/env python3
"""Apply paperskill.tablesnap to the transcriptions already saved in table_tests/.

No model calls: each case's result.json holds the vision model's rows, and the PDF's text
layer is read locally. Writes, per case, table_snapped.csv / table_snapped.md /
snap.json (every change, every unmatched token, before/after check), and
table_tests/SNAP_INDEX.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paperskill.accounting import native_words  # noqa: E402
from paperskill.tablesnap import snap_rows, text_direction  # noqa: E402
from paperskill.tabletx import PAD_PT, TableRead, check_rows, to_csv, to_markdown, verdict  # noqa: E402

OUT = ROOT / "table_tests"
sys.path.insert(0, str(ROOT / "scripts"))
from table_fallback_test import CASES, find  # noqa: E402


def main() -> None:
    lines = ["# Snap test", "",
             "| case | verdict before | verdict after | coverage | precision | snaps | paired | unmatched | skipped |",
             "|---|---|---|---|---|---|---|---|---|"]
    for run, page, item_id in CASES:
        name = f"{run.removesuffix('-review')}-p{page}-{item_id}"
        res = OUT / name / "result.json"
        if not res.exists():
            continue
        rec = json.loads(res.read_text())
        pdf, _ = find(run, page, item_id)
        frame = text_direction(pdf, page, rec["box"])
        words = frame.words(native_words(pdf, page))
        box = frame.box([rec["box"][0] - PAD_PT, rec["box"][1] - PAD_PT,
                         rec["box"][2] + PAD_PT, rec["box"][3] + PAD_PT])
        s = snap_rows(rec.get("raw_rows") or rec["rows"], words, box)
        before = rec["check"]
        after = check_rows(s["rows"], words, box)
        read = TableRead(rows=s["rows"], cut_off=rec["cut_off"], uncertain=[], notes="")
        v = verdict(read, after)
        (OUT / name / "table_snapped.csv").write_text(to_csv(s["rows"]))
        (OUT / name / "table_snapped.md").write_text(to_markdown(s["rows"]))
        (OUT / name / "snap.json").write_text(json.dumps({
            "verdict_before": rec["verdict"], "verdict_after": v,
            "check_before": before, "check_after": after,
            "snaps": s["snaps"], "paired": s["paired"], "unmatched": s["unmatched"], "skipped": s["skipped"]},
            indent=1, ensure_ascii=False))
        lines.append(f"| [{name}]({name}/snap.json) | {rec['verdict']} | {v} | "
                     f"{before['coverage']} -> {after['coverage']} | "
                     f"{before['precision']} -> {after['precision']} | {len(s['snaps'])} | "
                     f"{len(s['paired'])} | {len(s['unmatched'])} | {s['skipped'] or '-'} |")
    (OUT / "SNAP_INDEX.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
