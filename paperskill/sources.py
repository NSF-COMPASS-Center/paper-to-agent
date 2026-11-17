"""Keep a copy of each paper's original PDF next to its package, for comparison.

It goes to `<dist>/_sources/<name>.pdf`, not inside `<dist>/<name>/`: the builder
records every file of a package in a manifest and `verify` refuses a package whose file
set differs, so an extra file inside it would break verification. The same reasoning
put `<name>.LIMITATIONS.md` beside the package.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

SOURCES_DIR = "_sources"


def original_pdf(work: Path, ocr_source: str | None = None) -> Path | None:
    """The PDF the user supplied, from the run's inventory. A run that OCR'd pages
    prepared a modified copy, so `ocr_source` (the real input) wins when given."""
    if ocr_source and Path(ocr_source).exists():
        return Path(ocr_source)
    try:
        inv = json.loads((work / "inventory.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for src in inv.get("sources", []):
        if src.get("kind") == "pdf":
            for key in ("original_path", "snapshot"):
                p = Path(src.get(key) or "")
                p = p if p.is_absolute() else work / p
                if p.exists():
                    return p
    return None


def copy_source(output: Path, original: Path) -> Path:
    dest = output.parent / SOURCES_DIR / f"{output.name}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dest.exists() and original.resolve() == dest.resolve():
            return dest              # the PDF was put in _sources first; copying a file onto itself raises
    except OSError:
        pass
    shutil.copy2(original, dest)
    return dest
