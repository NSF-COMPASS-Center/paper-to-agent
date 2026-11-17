"""Copy each existing package's original PDF to dist/_sources/<name>.pdf.

Run once after a batch finishes (new runs do this themselves). Safe to repeat.
    python scripts/collect_sources.py [dist]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperskill.index import record_from_report, update_index  # noqa: E402
from paperskill.sources import copy_source, original_pdf  # noqa: E402

dist = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist").resolve()
index = {}
try:
    index = {r["name"]: r for r in json.loads((dist / "INDEX.json").read_text())}
except (OSError, json.JSONDecodeError):
    pass
for pkg in sorted(p for p in dist.iterdir() if p.is_dir() and not p.name.startswith("_")):
    work = Path((index.get(pkg.name) or {}).get("work") or ROOT / "runs" / f"{pkg.name}-review")
    row = index.get(pkg.name)
    if row is None:       # packages built before the index existed: rebuild the row from the run
        try:
            rep = json.loads((work / "agent-run" / "report.json").read_text())
            row = record_from_report(rep, built_ts=(work / "agent-run" / "report.json").stat().st_mtime)
            row["name"], row["package"] = pkg.name, str(pkg)
        except (OSError, json.JSONDecodeError):
            row = {"name": pkg.name, "status": "unknown", "package": str(pkg)}
    orig = original_pdf(work)
    if orig is None:
        print(f"{pkg.name}: no original found under {work}")
        continue
    row["source"] = str(copy_source(pkg, orig))
    row.setdefault("package", str(pkg))
    update_index(dist, row)
    print(f"{pkg.name}: {orig.name[:70]} -> _sources/{pkg.name}.pdf")
