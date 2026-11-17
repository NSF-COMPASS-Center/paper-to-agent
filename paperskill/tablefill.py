"""Fill in the tables the page reviewer left as images.

A table with no rows ships as a picture, which cannot be searched or computed on. After
the page reviews and the crop repair, this reads each such table with `tabletx`
(vision read -> snapped to the PDF text -> checked against it) and writes the rows
through `PageEditor.set_rows`.

Policy:
  * every verdict is written, including `rejected`, so a package holds a table rather
    than a picture; anything but a clean `accepted` is named in the package notes;
  * one floor: a checked read that covers under `min_coverage` of the PDF's own words (a
    refusal, a header-only read) leaves the image, because a one-row CSV would replace the
    table and lose it;
  * a failed call leaves the image and is not retried this run.

Each item is stamped with `table_fallback`, so a second build round does not pay again.
The title and footnote lines the model was told to leave out are recorded in the stamp
and the page review notes, because word accounting counts everything inside a crop as
carried.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .editor import EditError, PageEditor
from .tabletx import transcribe_table

CLEAN = {"accepted"}


def pending(plan_dirs: list[Path]) -> list[tuple[Path, Path, str, int, list[float]]]:
    """(source dir, page plan, item id, page, bbox) of every table kept as an image
    that has not already had its fallback."""
    out = []
    for sd in plan_dirs:
        if not (sd / "source.pdf").exists():
            continue
        for pp in sorted((sd / "pages").glob("page-*.json")):
            ed = PageEditor(pp)
            for it in ed.items:
                if (it.get("kind") == "table" and not it.get("rows")
                        and not it.get("table_fallback") and len(it.get("bbox") or []) == 4):
                    out.append((sd, pp, it["id"], int(ed.data["page"]), it["bbox"]))
    return out


def stamp(rec: dict) -> dict:
    c = rec["check"]
    return {"verdict": rec["verdict"], "coverage": c["coverage"], "precision": c["precision"],
            "snaps": len(rec.get("snaps") or []) + len(rec.get("paired") or []),
            "unmatched": [u["token"] for u in (rec.get("unmatched") or [])][:12],
            "missing": c["missing"][:8], "invented": c["invented"][:8],
            "excluded_lines": c.get("excluded_lines") or [],
            "cut_off": rec.get("cut_off") or [], "uncertain": (rec.get("uncertain") or [])[:6]}


def note_for(item: dict, page: int, s: dict) -> str:
    label = item.get("label") or item.get("id")
    bits = [f"{label} (page {page}) was read from the page image by the table fallback "
            f"and cross-checked against the PDF text: {s['verdict']}"]
    if s["coverage"] is not None:
        bits.append(f"{s['coverage']:.0%} of the PDF's words present, "
                    f"{s['precision']:.0%} of its words in the PDF")
    if s["missing"]:
        bits.append("missing: " + ", ".join(s["missing"][:5]))
    if s["invented"]:
        bits.append("not in the PDF: " + ", ".join(s["invented"][:5]))
    if s["cut_off"]:
        bits.append("the table runs past the crop at the " + "/".join(s["cut_off"]))
    if s["unmatched"]:
        bits.append("unverified cell text: " + ", ".join(s["unmatched"][:5]))
    if s["excluded_lines"]:
        bits.append("title/footnote not in the CSV: " + " | ".join(s["excluded_lines"][:3]))
    return "; ".join(bits) + ". Check it against the source PDF."


def apply(pp: Path, item_id: str, rec: dict, min_coverage: float) -> dict:
    """Write one result into its page plan. Returns the report record."""
    ed = PageEditor(pp)
    it = ed.get(item_id)
    s = stamp(rec)
    out = {"page": ed.data["page"], "item": item_id, **s, "applied": False}
    c = rec["check"]
    if not rec["rows"] or (c["checked"] and (c["coverage"] or 0) < min_coverage):
        out["why"] = ("the read was empty" if not rec["rows"] else
                      f"covered {c['coverage']:.0%} of the PDF's words, under the "
                      f"{min_coverage:.0%} floor; the image stays")
        it["table_fallback"] = {**s, "kept_image": True}
        ed.save()
        return out
    ed.set_rows(item_id, rec["rows"])
    it.pop("image_fallback_reason", None)
    it["table_fallback"] = s
    note = f"Table fallback: {s['verdict']}, {len(rec['rows'])} rows"
    if s["verdict"] not in CLEAN:
        note += " (see the package notes)"
    ed.data["review_notes"] = ((ed.data.get("review_notes") or "").rstrip() + " " + note + ".").strip()
    ed.ops.append(f"table_fallback({item_id}, {s['verdict']})")
    ed.save()
    out.update(applied=True, note=note_for(it, ed.data["page"], s), rows=len(rec["rows"]))
    return out


async def fill_image_tables(plan_dirs: list[Path], cfg, ledger, client,
                            concurrency: int = 3) -> list[dict]:
    jobs = pending(plan_dirs)
    sem = asyncio.Semaphore(max(1, concurrency))
    locks: dict[Path, asyncio.Lock] = {}

    async def one(sd: Path, pp: Path, item_id: str, page: int, bbox: list[float]) -> dict:
        async with sem:
            try:
                rec = await transcribe_table(client, cfg.vision_model, sd / "source.pdf", page,
                                             bbox, cfg.transcribe_dpi, ledger,
                                             unit=f"{sd.name}-p{page}")
            except Exception as e:           # an API or render failure leaves the image
                err = f"{type(e).__name__}: {e}"
                async with locks.setdefault(pp, asyncio.Lock()):
                    try:
                        ed = PageEditor(pp)
                        ed.get(item_id)["table_fallback"] = {"error": err, "kept_image": True}
                        ed.save()
                    except (EditError, OSError):
                        pass
                return {"page": page, "item": item_id, "applied": False, "why": err}
        async with locks.setdefault(pp, asyncio.Lock()):
            try:
                return apply(pp, item_id, rec, cfg.table_min_coverage)
            except EditError as e:           # the editor refused the page: keep the image
                return {"page": page, "item": item_id, "applied": False,
                        "why": f"the editor refused the rows: {e}"}

    return list(await asyncio.gather(*(one(*j) for j in jobs)))
