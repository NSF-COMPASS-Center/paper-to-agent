"""Word accounting: is every word the PDF carries somewhere in the page plan?

The engine's verifier compares lines and numbers, so a lost word is caught only when
it happens to form a missing line or a differing number. Two real losses slipped
that way: a table crop cut a few points short (paper 3 p27 lost "159" and "n=90.")
and a reviewer omitted fragments of a garbled text layer as "duplicates" (paper 15
p7 lost "rainfall", "incidence", "Borchardt", "hepatitis").

This asks the direct question. Every native word is either inside a figure/table/
formula crop, or present in the text the plan retains, or it is *unaccounted*. An
unaccounted word is a candidate loss. Each one is labelled with its likely cause so
the caller can act: `near_crop` (a crop edge is probably too tight), `omitted` (an
omit removed text that is retained nowhere), `uncovered` (no item claims it).

Pure and SDK-free. pymupdf is imported lazily, and only by `native_words`.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

CROP_KINDS = {"figure", "table", "formula"}
# Items an omit may drop without the text surviving elsewhere.
DECORATIVE = {"page-header", "page-footer"}
NEAR_CROP_PT = 15.0
MIN_ALPHA = 4           # shorter alphabetic tokens match too easily by substring

# An omit whose reason says one of these removed furniture, not content. "duplicate"
# is deliberately absent: that is the reason a reviewer gave when it dropped real text.
FURNITURE = re.compile(r"running (head|header|title)|page number|header|footer|watermark|"
                       r"copyright|permission|masthead|journal (name|title)", re.I)

_ALNUM = re.compile(r"[^0-9a-z]+")
_NUM_SPLIT = re.compile(r"[^0-9a-z.]+")
# Real markup only. A bare "<" in prose ("p < 0.05 ... >") must not swallow what follows.
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]{0,9}(?:\s[^<>]{0,60})?/?>")


def clean(text: str) -> str:
    """NFKC folds ligatures (ﬁ -> fi) and superscript digits; tags are markup."""
    return unicodedata.normalize("NFKC", _TAG.sub(" ", text)).lower()


def norm(text: str) -> str:
    return _ALNUM.sub("", clean(text))


def _item_text(it: dict) -> str:
    rows = it.get("rows") or []
    cells = " ".join(str(c) for r in rows for c in r)
    return f"{it.get('markdown') or ''} {cells}"


def _furniture(it: dict) -> bool:
    return (it.get("source_class") in DECORATIVE
            or bool(FURNITURE.search(it.get("reason") or "")))


def _inside(word: tuple, box: list[float], slack: float = 0.0) -> bool:
    cx, cy = (word[0] + word[2]) / 2, (word[1] + word[3]) / 2
    return box[0] - slack <= cx <= box[2] + slack and box[1] - slack <= cy <= box[3] + slack


def _dist_to(word: tuple, box: list[float]) -> float:
    dx = max(box[0] - word[2], 0, word[0] - box[2])
    dy = max(box[1] - word[3], 0, word[1] - box[3])
    return (dx * dx + dy * dy) ** 0.5


def account_page(words: list[tuple], items: list[dict]) -> dict:
    """`words` are (x0, y0, x1, y1, text) in the same space as the item bboxes."""
    crops = [it["bbox"] for it in items
             if it.get("kind") in CROP_KINDS and len(it.get("bbox") or []) == 4]
    kept = [it for it in items if it.get("kind") != "omit"]
    omits = [it for it in items if it.get("kind") == "omit"
             and len(it.get("bbox") or []) == 4]
    furniture = [o["bbox"] for o in omits if _furniture(o)]
    omitted = [o for o in omits if not _furniture(o)]
    text = " ".join(_item_text(it) for it in kept)
    blob = norm(text)
    tokens = {t.strip(".") for t in _NUM_SPLIT.split(clean(text)) if t.strip(".")}
    tokens |= {norm(t) for t in tokens}
    seq = [t for t in _NUM_SPLIT.split(clean(text)) if t]
    joins = {norm("".join(seq[i:i + k])) for k in (2, 3) for i in range(len(seq) - k + 1)}

    # A bare number inside a kept item's box must be in THAT item: "158" appearing
    # elsewhere on the page does not account for the 158 printed beside this one.
    owners = []
    for it in kept:
        if it.get("kind") in CROP_KINDS or len(it.get("bbox") or []) != 4:
            continue
        toks = {t.strip(".") for t in _NUM_SPLIT.split(clean(_item_text(it))) if t.strip(".")}
        owners.append((it["bbox"], toks | {norm(t) for t in toks}, norm(_item_text(it))))

    lost: list[dict] = []
    checked = 0
    for w in words:
        word = w[4]
        n = norm(word)
        if not n or (n.isalpha() and len(n) < MIN_ALPHA):
            continue
        if any(_inside(w, c) for c in crops) or any(_inside(w, f) for f in furniture):
            continue
        checked += 1
        # A short bare number must match as a whole token ("159" is not in "1159");
        # anything longer or mixed ("10−5)," "PACl-1.5s") is matched as a substring so
        # punctuation and sub/superscript markup around it do not matter.
        if n.isdigit() and len(n) <= 4:
            parts = [norm(p) for p in re.split(r"[^0-9a-zA-Z]+", clean(word)) if norm(p)]
            # "1–3" is two tokens, and "106" is 10 followed by a superscript 6.
            # Split items keep overlapping boxes, so a word can sit in several: it is
            # accounted for if ANY item whose box holds it carries the number.
            mine = [(t, bl) for bb, t, bl in owners if _inside(w, bb)]
            local = set().union(*(t for t, _ in mine)) if mine else tokens
            found = n in local or n in joins or all(p in local for p in parts)
            if not found and mine and not re.fullmatch(r"\W*\d+\W*", word):
                found = any(n in bl for _, bl in mine)   # "10.1%", "(5.5"
        else:
            found = n in blob
        if found:
            continue
        if any(_inside(w, o["bbox"]) for o in omitted):
            cause = "omitted"
        elif any(_dist_to(w, c) <= NEAR_CROP_PT for c in crops):
            cause = "near_crop"
        else:
            cause = "uncovered"
        lost.append({"word": word, "bbox": [round(v, 1) for v in w[:4]], "cause": cause})
    lost = _without_line_numbers(lost)
    return {"words": len(words), "checked": checked, "unaccounted": lost}


def _without_line_numbers(lost: list[dict]) -> list[dict]:
    """Manuscript line numbers (paper 3 pp17-23) are a column of bare integers down the
    margin. Five or more sharing a left edge are furniture, not content."""
    nums = [u for u in lost if re.fullmatch(r"\d{1,3}", u["word"])]
    drop: set[int] = set()
    for u in nums:
        col = [v for v in nums if abs(v["bbox"][0] - u["bbox"][0]) <= 3]
        if len(col) >= 5:
            drop.update(id(v) for v in col)
    return [u for u in lost if id(u) not in drop]


def garbled_items(items: list[dict]) -> list[str]:
    """Ids of items whose text the extractor emitted one word per line, as
    `word<br> word<br> ...` (paper 15 p7). That is a layout accident, not prose."""
    out = []
    for it in items:
        md = it.get("markdown") or ""
        breaks = md.count("<br>")
        if breaks >= 15 and len(md.split()) / (breaks + 1) <= 1.6:
            out.append(it.get("id", ""))
    return out


def native_words(pdf: Path, page: int) -> list[tuple]:
    """Words of a 1-based page; empty when the page has no text layer."""
    import pymupdf
    with pymupdf.open(str(pdf)) as doc:
        pg = doc[page - 1]
        words = [(w[0], w[1], w[2], w[3], w[4]) for w in pg.get_text("words")]
    return words


def account_plan(pdf: Path, plan: dict) -> dict:
    words = native_words(pdf, int(plan["page"]))
    out = account_page(words, plan.get("items") or [])
    out["page"] = plan["page"]
    out["garbled_items"] = garbled_items(plan.get("items") or [])
    out["no_text_layer"] = not words
    return out


def account_work(plan_dirs: list[Path]) -> list[dict]:
    """Account every reviewed-or-not page of every source; only pages with a loss
    are returned, so the report stays small and a clean paper is an empty list."""
    out: list[dict] = []
    for sd in plan_dirs:
        pdf = sd / "source.pdf"
        if not pdf.exists():
            continue
        for pp in sorted((sd / "pages").glob("page-*.json")):
            plan = json.loads(pp.read_text())
            try:
                acc = account_plan(pdf, plan)
            except Exception as e:      # a damaged page must not stop the run
                out.append({"source": sd.name, "page": plan.get("page"),
                            "error": f"{type(e).__name__}: {e}"})
                continue
            if acc["unaccounted"] or acc["garbled_items"]:
                causes: dict[str, int] = {}
                for u in acc["unaccounted"]:
                    causes[u["cause"]] = causes.get(u["cause"], 0) + 1
                out.append({"source": sd.name, "page": acc["page"], "causes": causes,
                            "words": [u["word"] for u in acc["unaccounted"][:12]],
                            "garbled_items": acc["garbled_items"]})
    return out
