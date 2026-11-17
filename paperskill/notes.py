"""The notes that used to end `paper.md`, as a separate artifact.

The builder prints bundle.json `notes` as a "Conversion notes" section at the end of
the paper's own text. Those notes are the harness's and the reviewer's commentary
(figures that were not extracted, repairs, OCR, table-fallback verdicts, draft
limitations), not the paper's content, so they do not belong in the reading document.

bundle.json `notes` stays the working store: the route and synthesis stages write it,
and the draft, OCR and table-fallback steps append to it. `Bundle.build` hides it from
the builder, so the package carries none of it, and this module writes it to
`<name>.NOTES.md` beside the package. It cannot go inside: the builder records every
file of a package in a manifest and `verify` refuses a package with a different file set.

Pure and SDK-free.
"""
from __future__ import annotations

import json
from pathlib import Path


def page_review_notes(plan_dirs: list[Path]) -> list[dict]:
    """Each reviewed page's own notes, in document order."""
    out = []
    for sd in plan_dirs:
        for pp in sorted((sd / "pages").glob("page-*.json")):
            try:
                data = json.loads(pp.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            text = (data.get("review_notes") or "").strip()
            if text:
                out.append({"source": sd.name, "page": data.get("page"), "notes": text})
    return out


def render_notes(name: str, notes: list[str], pages: list[dict]) -> str:
    lines = [f"# {name}: conversion and review notes", "",
             "These notes describe how the package was made and what to be careful about. "
             "They are kept out of the package's own documents; the paper's text is in "
             f"`{name}/references/paper.md`.", ""]
    lines += ["## Conversion notes", ""]
    lines += [f"- {str(n).strip()}" for n in notes] or ["None recorded."]
    lines += ["", "## Page review notes", "",
              "What the page reviewer reported for each page. The operations it actually "
              "performed are listed after `Changes:`; treat prose claims without a matching "
              "operation with caution.", ""]
    if not pages:
        lines.append("None recorded.")
    for p in pages:
        lines += [f"### {p['source']}, page {p['page']}", "", p["notes"], ""]
    return "\n".join(lines).rstrip() + "\n"


def write_notes(output: Path, name: str, notes: list[str], plan_dirs: list[Path]) -> Path:
    dest = output.parent / f"{output.name}.NOTES.md"
    dest.write_text(render_notes(name, notes, page_review_notes(plan_dirs)))
    return dest
