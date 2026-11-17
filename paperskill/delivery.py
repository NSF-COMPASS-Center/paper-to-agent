"""Inspection of the BUILT package.

`paper2skill`'s own instructions require this and it was never implemented:

> Before delivery, inspect the generated index, representative prose, captions,
> tables, and the smallest figure labels. Answer one realistic retrieval question
> using only the package.

Nothing else in the pipeline ever opens the finished package. That is how a
`references/index.md` containing two rows -- the title and the author line --
shipped as a completed conversion: every page verified, every character present,
and the navigation table useless.

Deterministic and SDK-free, so it runs in CI and costs nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# An asset the review never named keeps the extractor's positional id.
UNLABELLED = re.compile(r"^(figure|table|formula)-p\d{4}-\d{3}$")
# The asset-link grammar the downstream extractor (sciex_agent_v1) accepts; see
# PACKAGE_CONTRACT.md. A link outside it is silently not an asset there.
ASSET_LINK = re.compile(r"\[([^\]]*)\]\(\.\./(assets/[^)\s]+)\)")
ASSET_PATH_OK = re.compile(r"^assets/(figure|table|supp_figs|supp_table)/[A-Za-z0-9_.-]+\.(jpg|jpeg|png|csv)$")
# A row of the navigation table: | heading | purpose |
NAV_ROW = re.compile(r"^\|\s*(?P<heading>[^|]+?)\s*\|\s*(?P<purpose>[^|]*?)\s*\|\s*$")
BOILERPLATE_ROWS = {"exact heading", "--- ", "---", "document beginning"}


def headings(markdown: str) -> list[tuple[int, str]]:
    """Level and text of each ATX heading outside fenced blocks.

    Mirrors `reading_package.headings` so the counts being compared are the same
    ones the builder used.
    """
    out: list[tuple[int, str]] = []
    fence: str | None = None
    for line in markdown.splitlines():
        m = re.match(r"^\s*(`{3,}|~{3,})", line)
        if m:
            marker = m[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            h = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
            if h:
                out.append((len(h[1]), h[2]))
    return out


def nav_rows(index_md: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in index_md.splitlines():
        m = NAV_ROW.match(line)
        if not m:
            continue
        heading = m.group("heading").strip()
        if (not heading or heading.lower() in BOILERPLATE_ROWS
                or set(heading) <= set("-: ")):
            continue
        rows.append((heading, m.group("purpose").strip()))
    return rows


def inspect_package(pkg: Path, review_dir: Path | None = None) -> list[dict]:
    """Findings on a built package. status is 'fail', 'warn' or 'ok'."""
    pkg = Path(pkg)
    out: list[dict] = []

    def add(check: str, status: str, detail: str) -> None:
        out.append({"check": check, "status": status, "detail": detail})

    for required in ("SKILL.md", "references/index.md", "references/paper.md"):
        if not (pkg / required).is_file():
            add("required_files", "fail", f"missing {required}")
    if any(f["status"] == "fail" for f in out):
        return out

    paper = (pkg / "references/paper.md").read_text(encoding="utf-8")
    index = (pkg / "references/index.md").read_text(encoding="utf-8")
    hs = headings(paper)
    sections = [h for lvl, h in hs if lvl in (2, 3)]
    level1 = [h for lvl, h in hs if lvl == 1]
    rows = nav_rows(index)

    # The builder prepends its own `# <title>`, so level 1 is the document, not a
    # section. Extra level-1 headings mean sections were written at the wrong depth
    # and the index cannot see them.
    if len(level1) > 1:
        add("heading_levels", "fail",
            f"{len(level1)} level-1 headings in paper.md; the builder owns `#` for the "
            f"document title, so sections must be `##`/`###` or the index skips them. "
            f"Examples: {level1[1:4]}")

    if len(rows) < max(4, min(len(sections), 8)):
        add("index_rows", "fail",
            f"navigation table has {len(rows)} row(s) but paper.md has "
            f"{len(sections)} level-2/3 heading(s)")
    elif len(rows) < len(sections) * 0.5:
        add("index_rows", "warn",
            f"navigation table covers {len(rows)} of {len(sections)} sections")
    else:
        add("index_rows", "ok", f"{len(rows)} row(s) for {len(sections)} section(s)")

    if rows and not any(p for _, p in rows):
        add("index_purposes", "fail",
            "every 'Look here for' cell is empty; the builder leaves them blank unless "
            "bundle.json carries a curated `navigation` list")

    # assets
    asset_files = sorted(p for p in (pkg / "assets").rglob("*") if p.is_file()) \
        if (pkg / "assets").is_dir() else []
    unlabelled = [p.stem for p in asset_files if UNLABELLED.match(p.stem)]
    if unlabelled:
        add("asset_names", "fail",
            f"{len(unlabelled)} asset(s) never labelled during review, still carrying "
            f"extractor ids: {unlabelled[:6]}")
    else:
        add("asset_names", "ok", f"{len(asset_files)} asset(s), all named")

    docs = paper + (pkg / "references/supplement.md").read_text(encoding="utf-8") \
        if (pkg / "references/supplement.md").is_file() else paper
    orphans = [str(p.relative_to(pkg)) for p in asset_files
               if str(p.relative_to(pkg)) not in docs
               and p.name not in docs]
    if orphans:
        add("asset_links", "warn",
            f"{len(orphans)} asset(s) not linked from the documents: {orphans[:4]}")

    # downstream compatibility: what sciex can and cannot consume
    bad_links, mismatched, image_tables = [], [], []
    for label, path in ASSET_LINK.findall(docs):
        if not ASSET_PATH_OK.match(path):
            bad_links.append(path)
            continue
        folder, ext = path.split("/")[1], path.rsplit(".", 1)[1]
        says_table = bool(re.search(r"\btable", label, re.I))
        says_fig = bool(re.search(r"\bfig", label, re.I))
        if (says_table and folder in ("figure", "supp_figs")) or (says_fig and folder in ("table", "supp_table")):
            mismatched.append(f"{label!r} in {folder}/")
        if (says_table or folder in ("table", "supp_table")) and ext != "csv" and not label.lower().startswith("formula"):
            image_tables.append(Path(path).stem)
    problems = []
    if bad_links:
        problems.append(f"{len(bad_links)} asset link(s) outside the contract grammar (ignored by the "
                        f"extractor): {sorted(set(bad_links))[:4]}")
    if mismatched:
        problems.append(f"{len(mismatched)} asset(s) whose label and folder disagree (the extractor trusts "
                        f"the label): {mismatched[:3]}")
    if image_tables:
        problems.append(f"{len(set(image_tables))} table(s) kept as images, with no machine-readable values: "
                        f"{sorted(set(image_tables))[:5]}")
    if problems:
        add("sciex_compat", "warn", "; ".join(problems))
    else:
        add("sciex_compat", "ok", "asset links follow the package contract")

    # notes: the published caveats
    notes: list[str] = []
    if review_dir is not None:
        bj = Path(review_dir) / "bundle.json"
        if bj.is_file():
            try:
                notes = json.loads(bj.read_text()).get("notes") or []
            except (OSError, json.JSONDecodeError):
                notes = []
    if not notes:
        add("notes", "fail",
            "bundle.json has no notes. Notes are where a reader learns what is "
            "missing or caveated -- an appended corrigendum, a unit correction, a "
            "cited supplement that was not supplied, repairs made during review.")
    else:
        add("notes", "ok", f"{len(notes)} note(s)")

    title = ""
    m = re.search(r"^#\s+(.*\S)\s*$", paper, re.M)
    if m:
        title = m.group(1)
    if title and len(title) < 25 and not title[0].isupper():
        add("title", "warn",
            f"document title is {title!r}, which looks like the --name slug rather "
            "than the paper's title; pass --title or let synthesis set it")

    return out


def summarize(findings: list[dict]) -> tuple[int, int, str]:
    fails = sum(1 for f in findings if f["status"] == "fail")
    warns = sum(1 for f in findings if f["status"] == "warn")
    lines = [f"  [{f['status'].upper():>4}] {f['check']}: {f['detail']}"
             for f in findings if f["status"] != "ok"]
    oks = [f"  [  OK] {f['check']}: {f['detail']}"
           for f in findings if f["status"] == "ok"]
    return fails, warns, "\n".join(lines + oks)
