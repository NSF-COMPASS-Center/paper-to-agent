"""The tools the page reviewer gets. Each wraps a constrained editor op.

Every tool returns a string. An EditError becomes the tool result rather than an
exception, so a refused edit teaches the model what would be valid instead of
killing the run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agents import RunContextWrapper, function_tool

from .config import Config
from .editor import EditError, PageEditor
from .vision import transcribe_region


@dataclass
class PageContext:
    editor: PageEditor
    source_id: str
    page: int
    pdf: Path | None
    cfg: Config
    client: object
    ledger: object
    observations: list[str] = field(default_factory=list)
    transcribe_calls: int = 0
    limitations: list[str] = field(default_factory=list)


def _ok(ctx: PageContext, msg: str) -> str:
    return f"{msg}\n\ncurrent items:\n{json.dumps(ctx.editor.outline(), indent=1)}"


# --- inspection --------------------------------------------------------------

@function_tool
def note_observation(wrapper: RunContextWrapper[PageContext], description: str) -> str:
    """Record what you see on the page image before editing: column layout, what runs
    down each column, where figures and tables sit, and whether prose continues across
    the page boundary. Call this first, once."""
    c = wrapper.context
    if len(description.strip()) < 40:
        return ("Too brief. Describe the layout concretely: how many columns, what is in "
                "each, where the floats sit, and whether the first and last prose items "
                "continue across the page break.")
    c.observations.append(description.strip())
    return _ok(c, "Observation recorded.")


@function_tool
def get_item_text(wrapper: RunContextWrapper[PageContext], item_id: str) -> str:
    """Full extracted markdown of one item, for comparing against the image."""
    try:
        it = wrapper.context.editor.get(item_id)
    except EditError as e:
        return str(e)
    return json.dumps({k: it.get(k) for k in
                       ("id", "kind", "label", "asset_name", "join_previous", "bbox",
                        "markdown", "rows", "reason")}, indent=1, ensure_ascii=False)[:12000]


@function_tool
def get_extractor_evidence(wrapper: RunContextWrapper[PageContext]) -> str:
    """The raw extracted lines for this page, before the plan was assembled. Useful when
    the plan looks wrong and you need to see what the extractor actually got. This is a
    parser's output, not independent visual truth -- the image is the truth."""
    c = wrapper.context
    p = c.editor.path.parent.parent / "evidence" / c.editor.path.name
    if not p.exists():
        return "No evidence file for this page."
    return p.read_text()[:20000]


@function_tool
async def transcribe_item_region(wrapper: RunContextWrapper[PageContext],
                                 item_id: str, what_to_check: str) -> str:
    """Re-render one item's region of the source PDF at high resolution and transcribe it
    verbatim. Use this for single-character questions the page preview cannot settle:
    a corrupted comparison symbol or Greek letter, a superscript, or whether a hyphen is
    a compound hyphen or a line-wrap artefact. Say in `what_to_check` what you are
    looking for."""
    c = wrapper.context
    if c.pdf is None:
        return "The source PDF for this document is not available; decide from the page image."
    if c.transcribe_calls >= c.cfg.max_transcribe_calls_per_page:
        return (f"Budget of {c.cfg.max_transcribe_calls_per_page} high-resolution reads for "
                "this page is spent. Record anything still unresolved as a limitation in "
                "your review notes.")
    try:
        it = c.editor.get(item_id)
    except EditError as e:
        return str(e)
    bbox = it.get("bbox")
    c.transcribe_calls += 1
    try:
        r = await transcribe_region(c.client, c.cfg.vision_model, c.pdf, c.page, bbox,
                                    c.cfg.transcribe_dpi, what_to_check,
                                    ledger=c.ledger, unit=str(c.page))
    except Exception as e:                      # a render or API failure is not fatal
        return f"High-resolution read failed ({type(e).__name__}: {e}). Decide from the page image."
    return json.dumps(r.model_dump(), indent=1, ensure_ascii=False)


# --- edits -------------------------------------------------------------------

@function_tool
def reorder_items(wrapper: RunContextWrapper[PageContext], order: list[str]) -> str:
    """Put the page's items into correct reading order. Must list every current item id
    exactly once. For a two-column page that is normally the whole left column, then the
    whole right column, with each caption directly after its figure."""
    c = wrapper.context
    try:
        c.editor.reorder(order)
    except EditError as e:
        return str(e)
    return _ok(c, "Reordered.")


@function_tool
def set_item_markdown(wrapper: RunContextWrapper[PageContext],
                      item_id: str, markdown: str) -> str:
    """Replace one item's text to match the image. Correct extraction errors only --
    never paraphrase, reword or summarize what the page says."""
    c = wrapper.context
    try:
        c.editor.set_markdown(item_id, markdown)
    except EditError as e:
        return str(e)
    return _ok(c, f"Updated {item_id}.")


@function_tool
def split_item(wrapper: RunContextWrapper[PageContext], item_id: str, at_text: str) -> str:
    """Split one item in two where the extractor ran two paragraphs or two columns
    together. `at_text` is the exact text that should begin the second item."""
    c = wrapper.context
    try:
        a, b = c.editor.split(item_id, at_text)
    except EditError as e:
        return str(e)
    return _ok(c, f"Split into {a} and {b}.")


@function_tool
def merge_items(wrapper: RunContextWrapper[PageContext],
                item_ids: list[str], separator: str = " ") -> str:
    """Merge items the extractor fragmented, in the order given, into the first.
    Use separator "" where a word was broken, "\\n" for code lines."""
    c = wrapper.context
    try:
        kept = c.editor.merge(item_ids, separator)
    except EditError as e:
        return str(e)
    return _ok(c, f"Merged into {kept}.")


@function_tool
def set_item_kind(wrapper: RunContextWrapper[PageContext], item_id: str,
                  kind: str, reason: str = "") -> str:
    """Change an item's kind: heading, text, caption, code, figure, formula, table, omit.
    `omit` requires a specific verified reason and is only for page numbers, running
    headers and decorative marks."""
    c = wrapper.context
    try:
        c.editor.set_kind(item_id, kind, reason or None)
    except EditError as e:
        return str(e)
    return _ok(c, f"{item_id} is now {kind}.")


@function_tool
def set_join_previous(wrapper: RunContextWrapper[PageContext],
                      item_id: str, mode: str) -> str:
    """Join an item to the preceding prose: "space" if a sentence continues, "none" if a
    word itself is split, "null" to clear. Only after checking the render."""
    c = wrapper.context
    try:
        c.editor.set_join_previous(item_id, None if mode in ("null", "", "none_clear") else mode)
    except EditError as e:
        return str(e)
    return _ok(c, f"join_previous set on {item_id}.")


@function_tool
def expand_bbox(wrapper: RunContextWrapper[PageContext], item_id: str,
                side: str, points: float) -> str:
    """Grow one side of a figure, formula or table crop by `points` (PDF points, 1/72
    inch); a negative value shrinks it. Use when a panel, panel letter or axis label is
    cut off. Then call transcribe_item_region to check the new crop."""
    c = wrapper.context
    try:
        bb = c.editor.expand_bbox(item_id, side, points, c.cfg.bbox_nudge_max_pt)
    except EditError as e:
        return str(e)
    return _ok(c, f"{item_id} bbox is now {[round(v, 1) for v in bb]}.")


@function_tool
def set_asset(wrapper: RunContextWrapper[PageContext], item_id: str,
              label: str = "", asset_name: str = "", asset_category: str = "") -> str:
    """Set a figure or table's human label ("Figure 3"), its asset_name ("figure-3",
    lowercase and hyphenated), and optionally its category (figure, supp_figs, table,
    supp_table) for an extended-data figure printed inside the main document."""
    c = wrapper.context
    try:
        c.editor.set_asset(item_id, label or None, asset_name or None, asset_category or None)
    except EditError as e:
        return str(e)
    return _ok(c, f"Asset fields set on {item_id}.")


@function_tool
def set_table_rows(wrapper: RunContextWrapper[PageContext], item_id: str,
                   rows_json: str, fallback_reason: str = "") -> str:
    """Set a table's cells as a JSON array of equal-length string arrays, including the
    header row. Transcribe the table: merged headers are flattened by joining their
    parts with " / ". Pass "null" only when cells are genuinely unreadable on the
    render, and then give fallback_reason naming the specific cause -- an image-only
    table cannot be searched, so this is the exception, and the reason is recorded."""
    c = wrapper.context
    try:
        rows = json.loads(rows_json)
    except (json.JSONDecodeError, ValueError) as e:
        return f"rows_json is not valid JSON: {e}"
    try:
        c.editor.set_rows(item_id, rows, reason=fallback_reason or None)
    except EditError as e:
        return str(e)
    return _ok(c, f"Rows set on {item_id}.")


@function_tool
def record_limitation(wrapper: RunContextWrapper[PageContext], description: str) -> str:
    """Record something on this page that cannot be faithfully represented -- an
    illegible equation, a table with irreducible merged headers, a figure whose labels
    are unreadable at source resolution. Recording a limitation is a correct outcome."""
    c = wrapper.context
    c.limitations.append(description.strip())
    return "Limitation recorded; it will be carried into the review notes."


@function_tool
def mark_reviewed(wrapper: RunContextWrapper[PageContext], review_notes: str) -> str:
    """Finish this page. `review_notes` must name what you confirmed (reading order,
    captions, cross-page prose, figure crops) and every change you made. Generic notes
    are rejected."""
    c = wrapper.context
    notes = review_notes.strip()
    if c.limitations:
        notes += " Limitations: " + "; ".join(c.limitations)
    if c.editor.ops:
        notes += " Changes: " + "; ".join(c.editor.ops)
    try:
        c.editor.mark_reviewed(notes)
        c.editor.save()
    except EditError as e:
        return str(e)
    return "Page saved and marked reviewed. You are done; stop here."


INSPECT_TOOLS = [note_observation, get_item_text, get_extractor_evidence,
                 transcribe_item_region]
EDIT_TOOLS = [reorder_items, set_item_markdown, split_item, merge_items, set_item_kind,
              set_join_previous, expand_bbox, set_asset, set_table_rows]
PAGE_TOOLS = INSPECT_TOOLS + EDIT_TOOLS + [record_limitation, mark_reviewed]
