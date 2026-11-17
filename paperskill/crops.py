"""Crop completeness: does a figure or table crop contain everything that belongs to it?

The engine's pixel check confirms a crop matches the region the plan chose. It cannot
say the region is complete. Paper 3 p27: the table's row-label column sat 50 pt left
of the crop and its last rows ran under the crop's bottom edge, so "158"/"159" were
in no item, and nothing noticed until a number diff happened to surface.

Two signals, both from the PDF's own text layer (so $0 and model-free):
  * a word the package does not carry (`accounting`) that lines up with a crop's rows
    or columns, or sits next to its edge, is probably part of it;
  * a word the crop edge slices through is part of it by definition.

`repair_crops` grows only the flagged side, in `expand_bbox` steps, and re-checks. It
stops at another kept item and reports instead of guessing. Pure and SDK-free.
"""
from __future__ import annotations

from pathlib import Path

from .accounting import CROP_KINDS, NEAR_CROP_PT, account_page, native_words

PAD = 2.0            # margin added beyond the word being included
ALIGN_PT = 80.0      # how far a row-aligned word may sit from the crop and still belong
MAX_GROW = 80.0      # total growth allowed per side; beyond this a human decides
STEP = 24.0          # editor.expand_bbox cap per call
OVERLAP_TOL = 3.0    # tolerated overlap with a neighbouring kept item
ROUNDS = 4
STRADDLE = (0.15, 0.85)   # share of a word inside the crop that means "cut through"


def _overlap(a: list[float], b: list[float]) -> tuple[float, float]:
    return (min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1]))


def _share_inside(w: tuple, bb: list[float]) -> float:
    ox, oy = _overlap(list(w[:4]), bb)
    area = max((w[2] - w[0]) * (w[3] - w[1]), 1e-6)
    return max(ox, 0) * max(oy, 0) / area


def _inside_center(w: tuple, bb: list[float]) -> bool:
    cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
    return bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3]


def _attach(w: tuple, bb: list[float]) -> str | None:
    """The side of `bb` a word belongs to, or None."""
    share = _share_inside(w, bb)
    if STRADDLE[0] <= share <= STRADDLE[1]:
        # sliced: the side it pokes out of
        if w[0] < bb[0]:
            return "left"
        if w[2] > bb[2]:
            return "right"
        return "top" if w[1] < bb[1] else "bottom"
    if share > STRADDLE[1]:
        return None
    ox, oy = _overlap(list(w[:4]), bb)
    # same row band as the crop (a little slack below: a last row can run past the edge)
    if oy > 0 or bb[1] <= (w[1] + w[3]) / 2 <= bb[3] + NEAR_CROP_PT:
        if w[2] <= bb[0] and bb[0] - w[2] <= ALIGN_PT:
            return "left"
        if w[0] >= bb[2] and w[0] - bb[2] <= ALIGN_PT:
            return "right"
    cx = (w[0] + w[2]) / 2
    if bb[0] - PAD <= cx <= bb[2] + PAD:                              # same column
        if w[1] >= bb[3] and w[1] - bb[3] <= NEAR_CROP_PT:
            return "bottom"
        if w[3] <= bb[1] and bb[1] - w[3] <= NEAR_CROP_PT:
            return "top"
    return None


def crop_growth(words: list[tuple], items: list[dict]) -> tuple[dict, list[dict]]:
    """(growth, blocked). growth: item_id -> {side: points}; blocked: why a side could
    not be grown."""
    crops = [it for it in items if it.get("kind") in CROP_KINDS
             and len(it.get("bbox") or []) == 4]
    if not crops:
        return {}, []
    lost = {tuple(u["bbox"]): u for u in account_page(words, items)["unaccounted"]
            if u["cause"] != "omitted"}
    neighbours = [it for it in items if it.get("kind") != "omit"
                  and len(it.get("bbox") or []) == 4]
    need: dict[str, dict[str, float]] = {}
    for w in words:
        key = tuple(round(v, 1) for v in w[:4])
        # A word that belongs to a neighbouring text item is that item's, not the crop's.
        owned = any(_inside_center(w, n["bbox"]) and _share_inside(w, n["bbox"]) > STRADDLE[1]
                    for n in neighbours if n.get("kind") not in CROP_KINDS)
        straddles = not owned and any(
            STRADDLE[0] <= _share_inside(w, c["bbox"]) <= STRADDLE[1] for c in crops)
        if key not in lost and not straddles:
            continue
        best, best_d = None, 1e9
        for c in crops:
            side = _attach(w, c["bbox"])
            if side is None:
                continue
            d = min(abs(w[0] - c["bbox"][2]), abs(w[2] - c["bbox"][0]),
                    abs(w[1] - c["bbox"][3]), abs(w[3] - c["bbox"][1]))
            if d < best_d:
                best, best_d = (c, side), d
        if best is None:
            continue
        c, side = best
        bb = c["bbox"]
        pts = {"left": bb[0] - w[0], "right": w[2] - bb[2],
               "top": bb[1] - w[1], "bottom": w[3] - bb[3]}[side] + PAD
        if pts > 0:
            cur = need.setdefault(c["id"], {})
            cur[side] = max(cur.get(side, 0.0), pts)

    growth: dict[str, dict[str, float]] = {}
    blocked: list[dict] = []
    for c in crops:
        for side, pts in (need.get(c["id"]) or {}).items():
            bb = list(c["bbox"])
            if pts > MAX_GROW:
                blocked.append({"item": c["id"], "side": side,
                                "why": f"needs {pts:.0f} pt, over the {MAX_GROW:.0f} pt limit"})
                continue
            grown = list(bb)
            k = {"left": 0, "top": 1, "right": 2, "bottom": 3}[side]
            grown[k] += -pts if side in {"left", "top"} else pts
            hit = next((n["id"] for n in neighbours if n["id"] != c["id"]
                        and all(v > OVERLAP_TOL for v in _overlap(grown, n["bbox"]))), None)
            if hit:
                blocked.append({"item": c["id"], "side": side,
                                "why": f"growing it {pts:.0f} pt would overlap {hit}"})
                continue
            growth.setdefault(c["id"], {})[side] = pts
    return growth, blocked


def repair_crops(plan_dirs: list[Path], apply: bool = True) -> list[dict]:
    """Grow flagged crops via PageEditor; idempotent (a clean crop has nothing to grow)."""
    from .editor import EditError, PageEditor

    results: list[dict] = []
    for sd in plan_dirs:
        pdf = sd / "source.pdf"
        if not pdf.exists():
            continue
        for path in sorted((sd / "pages").glob("page-*.json")):
            try:
                ed = PageEditor(path)
                words = native_words(pdf, int(ed.data["page"]))
            except Exception:
                continue
            changed, seen = False, set()
            for _ in range(ROUNDS):
                growth, blocked = crop_growth(words, ed.items)
                for b in blocked:
                    key = (b["item"], b["side"], b["why"])
                    if key not in seen:
                        seen.add(key)
                        results.append({"source_id": sd.name, "page": ed.data["page"],
                                        "applied": False,
                                        "detail": f"{b['item']} {b['side']}: {b['why']}"})
                if not growth or not apply:
                    if growth:
                        results += [{"source_id": sd.name, "page": ed.data["page"],
                                     "applied": False,
                                     "detail": f"would grow {i} {s} by {p:.0f} pt"}
                                    for i, sides in growth.items() for s, p in sides.items()]
                    break
                for item_id, sides in growth.items():
                    for side, pts in sides.items():
                        left = pts
                        try:
                            while left > 0.01:
                                step = min(left, STEP)
                                ed.expand_bbox(item_id, side, round(step, 1), STEP)
                                left -= step
                        except EditError as exc:
                            results.append({"source_id": sd.name, "page": ed.data["page"],
                                            "applied": False, "detail": f"editor refused: {exc}"})
                            continue
                        changed = True
                        results.append({"source_id": sd.name, "page": ed.data["page"],
                                        "applied": True,
                                        "detail": f"grew {item_id} {side} by {pts:.0f} pt to "
                                                  "include text of the figure/table that the "
                                                  "crop cut off"})
            if changed:
                notes = (ed.data.get("review_notes") or "").rstrip()
                ed.data["review_notes"] = (
                    notes + " Deterministic repair: a figure or table crop that cut off "
                    "its own text was enlarged to include it; no content was changed."
                ).strip()
                try:
                    ed.save()
                except EditError as exc:
                    results.append({"source_id": sd.name, "page": ed.data["page"],
                                    "applied": False, "detail": f"save refused: {exc}"})
    return results
