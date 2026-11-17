"""Constrained edits to one page plan.

Two rules make this safe to hand to a model:

1. **No raw coordinates.** A bbox changes only through `expand_bbox(side, pt)`,
   capped per step. The model names a side and a distance, never a rectangle.
2. **No silent drops.** Every item id present when the page was loaded must
   still be accounted for when it is saved -- still present, or merged into
   another item, or explicitly turned into an `omit` with a reason. `validate()`
   enforces this and `save()` refuses a page that fails it.

Everything here is pure Python and unit-testable without an API key.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

KINDS = {"heading", "text", "caption", "code", "figure", "formula", "table", "omit"}
JOIN = {"space", "none", None}
SIDES = {"left", "top", "right", "bottom"}
HEADING_RE = re.compile(r"^#{1,6}\s+\S")


def describe_change(before: str, after: str, max_spans: int = 3) -> str:
    """Compact description of what a text edit actually changed.

    Character counts alone are not enough: a real repair can be
    length-preserving ('Barcelo' -> 'Barcelo\u0301' is 7 chars either way), so an op
    recorded as "18->18 chars" is indistinguishable from a no-op. Recording the
    changed spans makes a phantom repair visible in the audit trail.
    """
    spans: list[str] = []
    sm = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old, new = before[i1:i2], after[j1:j2]
        clip = lambda s: (s[:40] + "\u2026") if len(s) > 40 else s
        spans.append(f"@{i1} {clip(old)!r}->{clip(new)!r}")
        if len(spans) >= max_spans:
            spans.append("...")
            break
    return "; ".join(spans) if spans else "no change"


class EditError(ValueError):
    """Raised for an edit the model asked for that cannot be applied.

    The message goes back to the model as the tool result, so it should say what
    was wrong and what would be valid.
    """


class PageEditor:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())
        self.items: list[dict] = self.data.setdefault("items", [])
        self.original_ids = [i["id"] for i in self.items]
        self.absorbed: dict[str, str] = {}   # merged-away id -> surviving id
        self.ops: list[str] = []

    # --- lookup -------------------------------------------------------------
    def _idx(self, item_id: str) -> int:
        for n, it in enumerate(self.items):
            if it["id"] == item_id:
                return n
        raise EditError(f"no item {item_id!r} on this page; ids are "
                        f"{[i['id'] for i in self.items]}")

    def get(self, item_id: str) -> dict:
        return self.items[self._idx(item_id)]

    def outline(self) -> list[dict]:
        """Compact view for the model: no full markdown bodies."""
        out = []
        for it in self.items:
            md = it.get("markdown") or ""
            out.append({"id": it["id"], "kind": it.get("kind"),
                        "label": it.get("label"), "asset_name": it.get("asset_name"),
                        "join_previous": it.get("join_previous"),
                        "bbox": it.get("bbox"),
                        "chars": len(md),
                        "head": re.sub(r"\s+", " ", md)[:110],
                        "tail": re.sub(r"\s+", " ", md)[-60:] if len(md) > 170 else ""})
        return out

    # --- ops ----------------------------------------------------------------
    def reorder(self, order: list[str]) -> None:
        if sorted(order) != sorted(i["id"] for i in self.items):
            missing = set(i["id"] for i in self.items) - set(order)
            extra = set(order) - set(i["id"] for i in self.items)
            raise EditError(
                "reorder must list every current item id exactly once. "
                f"missing={sorted(missing)} unknown={sorted(extra)}")
        pos = {k: n for n, k in enumerate(order)}
        self.items.sort(key=lambda it: pos[it["id"]])
        self.ops.append(f"reorder({len(order)} items)")

    def hoist(self, ids: list[str], before: str | None = None,
              after: str | None = None) -> None:
        """Move a group of items (a figure/table and its caption) to sit immediately
        before or after one anchor item, keeping the group's own order. A thin layer
        over the same permutation `reorder` applies, so nothing can be dropped."""
        if (before is None) == (after is None):
            raise EditError("hoist needs exactly one of before= or after=")
        anchor = before if before is not None else after
        if anchor in ids:
            raise EditError("the anchor cannot be one of the items being moved")
        group = [self.items[self._idx(i)] for i in ids]
        rest = [it for it in self.items if it["id"] not in set(ids)]
        k = next((n for n, it in enumerate(rest) if it["id"] == anchor), None)
        if k is None:
            raise EditError(f"no item {anchor!r} on this page")
        k = k if before is not None else k + 1
        self.items[:] = rest[:k] + group + rest[k:]
        self.ops.append(f"hoist_float({'+'.join(ids)}, "
                        f"{'before' if before is not None else 'after'} {anchor})")

    def set_markdown(self, item_id: str, markdown: str) -> None:
        it = self.get(item_id)
        if it.get("kind") in {"figure", "formula", "table", "omit"}:
            raise EditError(f"{item_id} is kind={it['kind']}; its markdown is ignored by "
                            "the builder. Use set_rows for a table, or set_kind first.")
        before = it.get("markdown") or ""
        if not markdown.strip():
            raise EditError("markdown cannot be emptied; use set_kind(..., 'omit', reason=...)")
        if markdown == before:
            # A no-op edit recorded as a repair is worse than no edit: it puts a
            # change in the audit trail that never happened, and invites a review
            # note claiming work that was not done.
            raise EditError(
                f"{item_id} already contains exactly that text, so nothing was repaired. "
                "The extraction was already correct here. Do not record this as a repair; "
                "move on, and only call set_markdown where the plan actually disagrees "
                "with the rendered page.")
        it["markdown"] = markdown
        self.ops.append(f"set_markdown({item_id}, {len(before)}->{len(markdown)} chars, "
                        f"{describe_change(before, markdown)})")

    def split(self, item_id: str, at_text: str) -> tuple[str, str]:
        """Split one text item in two at the first occurrence of `at_text`,
        which begins the second part. Used where layout extraction ran two
        paragraphs or two columns together."""
        it = self.get(item_id)
        md = it.get("markdown") or ""
        k = md.find(at_text)
        if k <= 0:
            raise EditError(f"{at_text!r} not found inside {item_id} (or is at position 0). "
                            "Pass the exact text that should begin the second item.")
        n = self._idx(item_id)
        a, b = dict(it), dict(it)
        a["id"], b["id"] = f"{item_id}-a", f"{item_id}-b"
        a["markdown"], b["markdown"] = md[:k].rstrip(), md[k:].lstrip()
        b["join_previous"] = None
        self.items[n:n + 1] = [a, b]
        self.absorbed[item_id] = a["id"]
        self.ops.append(f"split({item_id} -> {a['id']},{b['id']})")
        return a["id"], b["id"]

    def merge(self, item_ids: list[str], separator: str = " ") -> str:
        """Merge adjacent items into the first, in the order given. For prose the
        extractor fragmented, or code split across items."""
        if len(item_ids) < 2:
            raise EditError("merge needs at least two item ids")
        idxs = [self._idx(i) for i in item_ids]
        keep = self.items[idxs[0]]
        kinds = {self.items[i].get("kind") for i in idxs}
        if len(kinds) > 1:
            raise EditError(f"refusing to merge different kinds {sorted(kinds)}; "
                            "set_kind them to match first if that is really intended")
        parts = [(self.items[i].get("markdown") or "") for i in idxs]
        keep["markdown"] = separator.join(p for p in parts if p)
        for i in sorted(idxs[1:], reverse=True):
            self.absorbed[self.items[i]["id"]] = keep["id"]
            del self.items[i]
        self.ops.append(f"merge({'+'.join(item_ids)} -> {keep['id']})")
        return keep["id"]

    def set_kind(self, item_id: str, kind: str, reason: str | None = None) -> None:
        if kind not in KINDS:
            raise EditError(f"kind must be one of {sorted(KINDS)}")
        it = self.get(item_id)
        if kind == "omit":
            if not reason or len(reason.strip()) < 10:
                raise EditError("omit requires a specific reason (e.g. 'running header "
                                "repeated on every page, verified against the render')")
            it["reason"] = reason.strip()
        it["kind"] = kind
        self.ops.append(f"set_kind({item_id}, {kind})")

    def set_join_previous(self, item_id: str, mode: str | None) -> None:
        if mode not in JOIN:
            raise EditError("join_previous must be 'space' (sentence continues), "
                            "'none' (a word continues) or null")
        it = self.get(item_id)
        if it.get("kind") == "code":
            raise EditError("code uses fenced blocks and cannot join to previous prose")
        if self._idx(item_id) == 0:
            raise EditError(f"{item_id} is the first item on the page; joining is only "
                            "meaningful where a preceding prose item exists")
        prev = self.items[self._idx(item_id) - 1]
        if prev.get("kind") not in {"text", "caption"}:
            raise EditError(f"the item before {item_id} is kind={prev.get('kind')}; "
                            "join_previous needs preceding prose (text or a caption). A paragraph that "
                            "follows a heading starts a new paragraph and is not joined.")
        it["join_previous"] = mode
        self.ops.append(f"set_join_previous({item_id}, {mode})")

    def expand_bbox(self, item_id: str, side: str, points: float, cap: float) -> list[float]:
        """Grow (or with a negative value shrink) one side of a figure/table crop.

        Points, not pixels: bboxes are in PDF points, 1/72 inch. Capped per call
        so a mistake is small and visible instead of catastrophic.
        """
        if side not in SIDES:
            raise EditError(f"side must be one of {sorted(SIDES)}")
        if abs(points) > cap:
            raise EditError(f"a single expand_bbox step is capped at {cap} pt; "
                            "call it again to go further, checking the render between")
        it = self.get(item_id)
        if it.get("kind") not in {"figure", "formula", "table"}:
            raise EditError(f"{item_id} is kind={it.get('kind')}; only figure, formula "
                            "and table items have a crop")
        bb = list(it.get("bbox") or [])
        if len(bb) != 4:
            raise EditError(f"{item_id} has no usable bbox")
        w = self.data.get("width") or self.data.get("page_width")
        h = self.data.get("height") or self.data.get("page_height")
        k = {"left": 0, "top": 1, "right": 2, "bottom": 3}[side]
        bb[k] += -points if side in {"left", "top"} else points
        bb[0], bb[1] = max(0.0, bb[0]), max(0.0, bb[1])
        if w:
            bb[2] = min(float(w), bb[2])
        if h:
            bb[3] = min(float(h), bb[3])
        if bb[2] - bb[0] < 10 or bb[3] - bb[1] < 10:
            raise EditError("that would collapse the crop below 10 pt; refused")
        it["bbox"] = bb
        self.ops.append(f"expand_bbox({item_id}, {side}, {points}pt)")
        return bb

    def set_asset(self, item_id: str, label: str | None = None,
                  asset_name: str | None = None,
                  asset_category: str | None = None) -> None:
        it = self.get(item_id)
        if asset_name is not None:
            if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", asset_name):
                raise EditError("asset_name must be lowercase, hyphenated, no extension "
                                "(e.g. 'figure-2', 'supplementary-figure-1')")
            it["asset_name"] = asset_name
        if label is not None:
            it["label"] = label
        if asset_category is not None:
            if asset_category not in {"figure", "supp_figs", "table", "supp_table"}:
                raise EditError("asset_category must be figure, supp_figs, table or supp_table")
            it["asset_category"] = asset_category
        self.ops.append(f"set_asset({item_id})")

    def set_rows(self, item_id: str, rows: list[list[str]] | None,
                 reason: str | None = None) -> None:
        it = self.get(item_id)
        if it.get("kind") != "table":
            raise EditError(f"{item_id} is not a table")
        if rows is None:
            if not reason or len(reason.strip()) < 30:
                raise EditError(
                    "A table kept as an image cannot be searched or computed on, so "
                    "transcribe it first: read every cell off the render, and flatten "
                    "merged headers by joining their parts with ' / '. Fall back to an "
                    "image only when cells are genuinely unreadable at the render "
                    "resolution, and then pass fallback_reason naming the specific "
                    "cause (which region, why) in at least a sentence.")
            it["rows"] = None            # keep as an image, with a limitation note
            it["image_fallback_reason"] = reason.strip()
        else:
            if not rows or any(len(r) != len(rows[0]) for r in rows):
                raise EditError("every row must have the same number of columns")
            if any(not isinstance(c, str) for r in rows for c in r):
                raise EditError("CSV cells must be strings, not numbers "
                                "(the stored string is the fidelity target)")
            it["rows"] = rows
        self.ops.append(f"set_rows({item_id}, "
                        f"{'image-fallback: ' + reason.strip()[:80] if rows is None else f'{len(rows)} rows'})")

    def mark_reviewed(self, notes: str) -> None:
        if len(notes.strip()) < 25:
            raise EditError("review_notes must say specifically what was checked and what "
                            "was changed on this page -- not 'looks fine'. Name the reading "
                            "order you confirmed and any repair you made.")
        self.data["reviewed"] = True
        self.data["review_notes"] = notes.strip()
        self.ops.append("mark_reviewed")

    # --- integrity ----------------------------------------------------------
    def validate(self) -> list[str]:
        problems: list[str] = []
        now = {i["id"] for i in self.items}
        if len(now) != len(self.items):
            problems.append("duplicate item ids on this page")
        for oid in self.original_ids:
            if oid in now or oid in self.absorbed:
                continue
            problems.append(f"item {oid} disappeared without a split, merge or omit")
        for it in self.items:
            if it.get("kind") == "omit" and not (it.get("reason") or "").strip():
                problems.append(f"{it['id']} is omit with no reason")
            if it.get("kind") not in KINDS:
                problems.append(f"{it['id']} has unknown kind {it.get('kind')!r}")
        live = [i for i in self.items if i.get("kind") != "omit"]
        for n, it in enumerate(live):
            if it.get("kind") == "heading" and not HEADING_RE.match(it.get("markdown") or ""):
                problems.append(f"{it['id']} is a heading but its markdown has no leading "
                                "'#'; the navigation index only sees '## ' / '### ' lines. "
                                "Use set_item_markdown to add them (## for 1., ### for 1.1).")
            if it.get("join_previous") and n and (
                    it.get("kind") not in {"text", "caption"}
                    or live[n - 1].get("kind") not in {"text", "caption"}):
                problems.append(f"{it['id']} has join_previous but follows a "
                                f"{live[n - 1].get('kind')} item; the builder joins only onto "
                                "text or a caption. Clear it with set_join_previous(null).")
        if self.data.get("reviewed") and len((self.data.get("review_notes") or "").strip()) < 25:
            problems.append("page marked reviewed without specific review_notes")
        problems.extend(self._lost_by_omit())
        return problems

    OMIT_LOSS_LIMIT = 4     # below this a stray word is extraction noise, not a dropped passage

    def _lost_by_omit(self) -> list[str]:
        """Text an `omit` removed that no retained item carries.

        Paper 15 p7: a garbled text layer was omitted fragment by fragment as
        "duplicate", but "rainfall", "incidence" and "hepatitis" were in none of the
        retained items. `omit` is only safe when the words survive elsewhere (or the
        item is furniture, which `accounting` exempts).
        """
        pdf = self.path.parent.parent / "source.pdf"
        if not pdf.exists() or not self.data.get("page"):
            return []
        from .accounting import account_plan
        try:
            acc = account_plan(pdf, self.data)
        except Exception:      # an unreadable PDF must not block a page save
            return []
        lost = [u["word"] for u in acc["unaccounted"] if u["cause"] == "omitted"]
        if len(lost) < self.OMIT_LOSS_LIMIT:
            return []
        return [f"omitted items removed {len(lost)} word(s) that no retained item contains "
                f"({', '.join(lost[:8])}). An omit is only valid when its text is already in "
                "another item. Merge that text into a retained item, restore the omitted "
                "item, or keep it and mark it as furniture only if it truly is a running "
                "header, footer or page number."]

    def save(self) -> None:
        problems = self.validate()
        if problems:
            raise EditError("refusing to save this page:\n- " + "\n- ".join(problems))
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
