"""Per-page review: one fresh agent session per page.

Context is scoped to a single page on purpose. Page plans are independent, the
page image is the expensive part of the context, and a session carried across
pages would resend every earlier image on every later request.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from agents import Agent, MaxTurnsExceeded, Runner

from .bundle import Bundle
from .config import Config
from .editor import PageEditor
from .prompts import page_instructions
from .tools import PAGE_TOOLS, PageContext
from .vision import encode_image


def page_already_reviewed(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text()).get("reviewed"))
    except (OSError, json.JSONDecodeError):
        return False


async def review_page(b: Bundle, cfg: Config, ledger, client,
                      source_id: str, page: int, *,
                      queue_notes: list[str] | None = None,
                      force: bool = False) -> dict:
    """Review one page. Returns a record for the run report."""
    path = b.page_path(source_id, page)
    if not path.exists():
        return {"source": source_id, "page": page, "status": "missing_plan"}
    if page_already_reviewed(path) and not force:
        return {"source": source_id, "page": page, "status": "cached"}

    preview = b.preview_path(source_id, page)
    if not preview.exists():
        return {"source": source_id, "page": page, "status": "missing_preview"}

    editor = PageEditor(path)
    ctx = PageContext(editor=editor, source_id=source_id, page=page,
                      pdf=b.source_pdf(source_id), cfg=cfg, client=client, ledger=ledger)

    agent = Agent[PageContext](name=f"page-{page}-reviewer",
                               instructions=page_instructions(),
                               model=cfg.review_model,
                               tools=PAGE_TOOLS)

    header = [f"Page {page} of document {source_id!r}.",
              f"Page size: {editor.data.get('width')} x {editor.data.get('height')} pt."
              f"  Extraction mode: {editor.data.get('mode')}."]
    if editor.data.get("warnings"):
        header.append("Extractor warnings for this page: "
                      + "; ".join(str(w) for w in editor.data["warnings"]))
    if queue_notes:
        header.append("The review queue flags, for this page: " + "; ".join(queue_notes))
    header.append("")
    header.append("Extracted plan, in its current order:")
    header.append(json.dumps(editor.outline(), indent=1, ensure_ascii=False))
    header.append("")
    header.append("The rendered page follows. It is the truth; the plan above is a guess "
                  "at it. Start with note_observation.")

    t0 = time.time()
    outcome = "ran"
    try:
        result = await Runner.run(
            agent,
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": "\n".join(header)},
                encode_image(preview)]}],
            context=ctx,
            max_turns=cfg.max_turns_per_page,
        )
        ledger.record_run_result(stage="page", unit=str(page), result=result,
                                 seconds=time.time() - t0, note=f"{source_id} p{page}")
        final = str(getattr(result, "final_output", ""))[:500]
    except MaxTurnsExceeded as exc:
        # AgentsException.run_data carries raw_responses and context_wrapper for the
        # turns that did complete. Without reading it, a capped page's tokens go
        # unrecorded and the run's cost is understated -- which is worse than the
        # cap itself, because it is invisible.
        rd = getattr(exc, "run_data", None)
        if rd is not None:
            ledger.record_run_result(stage="page", unit=str(page), result=rd,
                                     seconds=time.time() - t0,
                                     note=f"{source_id} p{page} capped")
        final = f"hit the {cfg.max_turns_per_page}-turn cap"
        outcome = "capped"

    # A run ends the moment the model emits text instead of a tool call, and a
    # capped run ends mid-thought -- either way the page can have every edit
    # applied and still not be marked reviewed, which blocks the build. One
    # bounded pass recovers it, starting from the editor's current state rather
    # than replaying the conversation.
    if not editor.data.get("reviewed"):
        t1 = time.time()
        tail = [
            f"Your review of page {page} stopped before you called mark_reviewed"
            + (f", after hitting the {cfg.max_turns_per_page}-turn cap." if outcome == "capped"
               else "."),
            f"Edits already applied and kept: {'; '.join(editor.ops) or 'none'}.",
            "",
            "Items as they now stand:",
            json.dumps(editor.outline(), indent=1, ensure_ascii=False),
            "",
            "Finish the page now. Call mark_reviewed with notes naming what you confirmed "
            "and what you changed -- describe only edits that actually went through. If "
            "something could not be resolved, call record_limitation first and then "
            "mark_reviewed anyway. Do not begin any new investigation.",
        ]
        try:
            fin = await Runner.run(
                agent,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": "\n".join(tail)},
                    encode_image(preview)]}],
                context=ctx, max_turns=6)
            ledger.record_run_result(stage="page", unit=str(page), result=fin,
                                     seconds=time.time() - t1,
                                     note=f"{source_id} p{page} finalize")
            final = str(getattr(fin, "final_output", ""))[:500] or final
        except MaxTurnsExceeded as exc:
            rd = getattr(exc, "run_data", None)
            if rd is not None:
                ledger.record_run_result(stage="page", unit=str(page), result=rd,
                                         seconds=time.time() - t1,
                                         note=f"{source_id} p{page} finalize capped")

    # Status comes from the page's actual state, never from which exception fired:
    # a capped run that nonetheless finished the page is reviewed.
    if editor.data.get("reviewed"):
        status = "reviewed"
    else:
        status = "max_turns" if outcome == "capped" else "incomplete"
        try:
            if not editor.validate():
                editor.save()      # keep validated edits for the next attempt
        except Exception:
            pass
    dt = time.time() - t0

    return {"source": source_id, "page": page, "status": status,
            "seconds": round(dt, 1), "ops": list(editor.ops),
            "transcribe_calls": ctx.transcribe_calls,
            "limitations": ctx.limitations, "final": final}


def queue_notes_by_page(queue: dict) -> dict[tuple[str, int], list[str]]:
    """Index review-queue.json by (source_id, page).

    Real shape, confirmed against the vendored review-aid output:

        {"schema_version": 1,
         "sources": [{"id": "s001-...", "title": ..., "pages": 13,
                      "contact_sheets": [...],
                      "items": [{"page": 1, "kind": "extractor_warning",
                                 "detail": "Review possible header/footer: p0001-b000"},
                                {"page": 2, "kind": "suggested_compact_omission",
                                 "item": "p0002-b000", "reason": "Repeated decorative ..."},
                                {"page": 1, "kind": "unreviewed_page"}]}]}

    `unreviewed_page` is dropped: it says only that the page has not been done
    yet, which the reviewer already knows and which would crowd out real flags.
    """
    out: dict[tuple[str, int], list[str]] = {}
    if not isinstance(queue, dict):
        return out
    for src in (queue.get("sources") or []):
        sid = src.get("id") or ""
        for item in (src.get("items") or []):
            kind = item.get("kind") or "flag"
            if kind == "unreviewed_page":
                continue
            page = int(item.get("page") or 0)
            detail = item.get("detail") or item.get("reason") or item.get("check") or ""
            ref = item.get("item")
            note = f"{kind}: {detail}" + (f" [{ref}]" if ref else "")
            out.setdefault((sid, page), []).append(note.strip())
    return out
