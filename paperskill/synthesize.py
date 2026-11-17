"""The one stage that sees the whole paper.

Everything else is scoped to a single page, which is why page-local work (crops,
reading order, cross-page joins) came out well while the document-level fields were
empty. `bundle.json`'s `navigation` and `notes` cannot be written from one page:

  * `navigation` gives each section a "Look here for" line. The builder fills that
    column with an empty string unless a curated list is supplied, so without this
    stage the index has headings and no purposes -- or, when heading depth is also
    wrong, no rows at all.
  * `notes` are published with the package and are where a reader learns what is
    missing or caveated: a cited supplement that was not supplied, an appended
    corrigendum and what it corrects, repairs made during review.

A heading that is not present verbatim in the built document stops the build, so
the model's proposal is validated before anything is written, retried once with the
problems named, and finally filtered rather than abandoned.
"""
from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from .bundle import (Bundle, NAV_MAX_ENTRIES, document_headings,
                     validate_navigation)
from .config import Config
from .prompts import synthesis_instructions


class NavEntry(BaseModel):
    file: Literal["references/paper.md", "references/supplement.md"]
    heading: str = Field(description="Copied character for character from the "
                                     "heading list you were given.")
    purpose: str = Field(description="Where information is, not what the findings "
                                     "are. Name the figures and tables it contains.")


class Synthesis(BaseModel):
    title: str = Field(description="The paper's real full title.")
    description: str = Field(description="One sentence on what this package contains.")
    navigation: list[NavEntry]
    notes: list[str] = Field(description="What a reader would be misled by without "
                                         "being told. Empty if genuinely nothing.")


def gather(b: Bundle) -> dict:
    """Everything the document-level pass needs, and nothing page-local."""
    headings = document_headings(b)
    assets: list[dict] = []
    page_notes: list[dict] = []
    for d in b.plan_dirs:
        for path in b.pages(d.name):
            try:
                plan = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            page = plan.get("page")
            if (plan.get("review_notes") or "").strip():
                page_notes.append({"page": page, "notes": plan["review_notes"][:900]})
            items = plan.get("items", [])
            for n, it in enumerate(items):
                if it.get("kind") not in {"figure", "table", "formula"}:
                    continue
                caption = ""
                for nxt in items[n + 1:n + 3]:
                    if nxt.get("kind") == "caption":
                        caption = (nxt.get("markdown") or "")[:300]
                        break
                assets.append({"page": page, "kind": it["kind"],
                               "label": it.get("label"),
                               "asset_name": it.get("asset_name"),
                               "caption": caption})
    return {"headings": headings, "assets": assets, "page_notes": page_notes,
            "tool_notes": b.bundle_json().get("notes") or []}


async def synthesize(b: Bundle, cfg: Config, ledger, client, log) -> dict:
    """Write navigation, notes, title and description into bundle.json."""
    material = gather(b)
    flat = [h for hs in material["headings"].values() for h in hs]
    if not flat:
        log("synthesis: no headings found in the page plans; skipping")
        return {"skipped": "no headings"}

    payload = {
        "headings": [{"file": h["file"], "level": h["level"], "heading": h["heading"],
                      "page": h["page"]} for h in flat],
        "assets": material["assets"],
        "page_review_notes": material["page_notes"],
        "notes_recorded_by_the_conversion_tool": material["tool_notes"],
        "navigation_entry_cap": NAV_MAX_ENTRIES,
    }

    async def ask(extra: str = "") -> Synthesis:
        t0 = time.time()
        resp = await client.responses.parse(
            model=cfg.review_model,
            instructions=synthesis_instructions(),
            input=[{"role": "user", "content":
                    (extra + "\n\n" if extra else "")
                    + json.dumps(payload, indent=1, ensure_ascii=False)[:140000]}],
            text_format=Synthesis,
        )
        ledger.record(stage="synthesize", unit="-", model=cfg.review_model,
                      usage=resp.usage, seconds=time.time() - t0,
                      note="retry" if extra else f"{len(flat)} headings")
        return resp.output_parsed

    out = await ask()
    entries = [e.model_dump() for e in out.navigation]
    problems = validate_navigation(entries, material["headings"])

    if problems:
        log(f"synthesis: {len(problems)} navigation problem(s); retrying once")
        for p in problems[:5]:
            log(f"  {p}")
        out = await ask(
            "Your previous navigation list was rejected. A heading that is not "
            "present verbatim stops the build, so copy each heading exactly from the "
            "`headings` list below. Problems:\n- " + "\n- ".join(problems[:20]))
        entries = [e.model_dump() for e in out.navigation]
        problems = validate_navigation(entries, material["headings"])

    dropped = 0
    if problems:
        # Keep what is valid rather than shipping no index at all.
        known = {f: {h["heading"] for h in hs}
                 for f, hs in material["headings"].items()}
        kept, seen = [], set()
        for e in entries:
            key = (e.get("file"), (e.get("heading") or "").strip())
            if (key[0] in known and key[1] in known[key[0]] and key not in seen):
                kept.append(e)
                seen.add(key)
        dropped = len(entries) - len(kept)
        entries = kept[:NAV_MAX_ENTRIES]
        log(f"synthesis: dropped {dropped} invalid navigation entr(ies), kept {len(entries)}")

    data = b.bundle_json()
    if out.title.strip():
        data["title"] = out.title.strip()
    data["notes"] = [n.strip() for n in out.notes if n.strip()]
    data["navigation"] = entries
    b.write_bundle_json(data)

    log(f"synthesis: {len(entries)} navigation entr(ies), {len(data['notes'])} note(s)")
    return {"navigation": len(entries), "notes": data["notes"],
            "title": data.get("title"), "description": out.description,
            "dropped_entries": dropped, "remaining_problems": problems}
