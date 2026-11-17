"""Snap a vision-model table transcription to the PDF's own text layer.

A vision read is good at table *structure* (which cell is in which row and column) and
unreliable at *characters*: paper 8's primer table came back with `ACAYAAY` for the
printed `ACACYAAY`. The PDF's text layer holds the exact characters but not the grid.
So the model supplies the grid and the PDF supplies the text.

For every whitespace token of every cell that the PDF does not carry verbatim, find the
single closest word the PDF has inside the table crop. Replace it only when that word is
unique and within a few character edits; otherwise leave the model's text and report the
token as unmatched. Every change is returned, so a snap can be audited and never hides.

No positions are used, so the rule is deliberately conservative: a token that already
matches some PDF word is never touched, and an ambiguous nearest word is never chosen.
Callers pass words and box already in the table's reading frame (`Frame`), so a table
printed sideways is snapped like any other.

Pure and SDK-free.
"""
from __future__ import annotations

from .accounting import norm

MIN_LEN = 4          # shorter tokens differ by one edit from too many unrelated words
MAX_REL = 0.12       # allowed edits as a share of the token's length
MAX_ABS = 3
PAIR_REL = 0.5       # second pass: edits allowed as a share of length
MAX_DIGIT_SHARE = 0.3   # second pass is for letter sequences; numbers are never paired by order
PAIR_WINDOW = 4      # leftover PDF words considered after the previous pairing


def edit_distance(a: str, b: str, cap: int = 4) -> int:
    """Levenshtein distance, returning cap+1 as soon as it must exceed `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def allowed_edits(n: int) -> int:
    return min(MAX_ABS, max(1, round(MAX_REL * n)))


def _native(words: list[tuple], box: list[float]) -> dict[str, set[str]]:
    """Words inside `box`, as {normalised: {raw spellings}}."""
    x0, y0, x1, y1 = box
    out: dict[str, set[str]] = {}
    for w in words:
        if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1:
            n = norm(w[4])
            if n:
                out.setdefault(n, set()).add(w[4])
    return out


def _joins(words: list[tuple], box: list[float]) -> set[str]:
    """Normalised joins of 2-3 consecutive PDF words in reading order. A printed
    "2.6E+03" is stored as "2.6E", a corrupted plus, "03"; the join is what the cell says.
    Words with no letters or digits (the corrupted plus) are skipped over."""
    x0, y0, x1, y1 = box
    ins = sorted((w for w in words
                  if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1),
                 key=lambda w: (round((w[1] + w[3]) / 2 / 3), w[0]))
    seq = [n for n in (norm(w[4]) for w in ins) if n]
    return {"".join(seq[i:i + k]) for k in (2, 3) for i in range(len(seq) - k + 1)}


class Frame:
    """The table's reading frame. A table printed sideways (paper 53 p9, text running
    bottom to top) has words whose boxes are tall and whose reading order runs along y.
    `Frame` rotates word and crop coordinates so the text reads left to right, top to
    bottom, which is what the line grouping, band splitting and snapping assume.

    direction is the unit vector the text runs along, in page coordinates (y down):
    (1, 0) upright, (0, -1) bottom-to-top, (0, 1) top-to-bottom, (-1, 0) upside down."""

    def __init__(self, direction: tuple[int, int] = (1, 0), width: float = 0.0,
                 height: float = 0.0):
        self.direction, self.w, self.h = tuple(direction), width, height

    @property
    def upright(self) -> bool:
        return self.direction == (1, 0)

    def rect(self, x0: float, y0: float, x1: float, y1: float) -> list[float]:
        d, W, H = self.direction, self.w, self.h
        if d == (0, -1):
            return [H - y1, x0, H - y0, x1]
        if d == (0, 1):
            return [y0, W - x1, y1, W - x0]
        if d == (-1, 0):
            return [W - x1, H - y1, W - x0, H - y0]
        return [x0, y0, x1, y1]

    def box(self, b: list[float]) -> list[float]:
        return self.rect(*b)

    def words(self, words: list[tuple]) -> list[tuple]:
        if self.upright:
            return words
        return [(*self.rect(*w[:4]), w[4]) for w in words]


def text_direction(pdf, page: int, box: list[float]) -> Frame:
    """The dominant text direction of the lines inside `box`, as a Frame."""
    import pymupdf
    with pymupdf.open(str(pdf)) as doc:
        pg = doc[page - 1]
        votes: dict[tuple[int, int], int] = {}
        for blk in pg.get_text("dict", clip=pymupdf.Rect(*box)).get("blocks", []):
            for ln in blk.get("lines", []):
                d = (round(ln["dir"][0]), round(ln["dir"][1]))
                n = sum(len(sp["text"]) for sp in ln["spans"])
                votes[d] = votes.get(d, 0) + n
        top = max(votes, key=votes.get) if votes else (1, 0)
        if top not in {(1, 0), (0, -1), (0, 1), (-1, 0)}:
            top = (1, 0)
        return Frame(top, pg.rect.width, pg.rect.height)


def snap_token(token: str, native: dict[str, set[str]],
               joins: frozenset[str] | set[str] = frozenset()) -> tuple[str | None, str]:
    """(replacement, reason). replacement is None when the token is left alone."""
    n = norm(token)
    if len(n) < MIN_LEN:
        return None, "short"
    if n in native or n in joins:
        return None, "exact"
    cap = allowed_edits(len(n))
    scored = sorted((edit_distance(n, k, cap), k) for k in native
                    if abs(len(k) - len(n)) <= cap)
    scored = [s for s in scored if s[0] <= cap]
    if not scored:
        return None, "unmatched"
    best = scored[0]
    if len(scored) > 1 and scored[1][0] == best[0]:
        return None, "ambiguous"
    spellings = native[best[1]]
    if len(spellings) != 1:
        return None, "ambiguous"
    rep = next(iter(spellings))
    if n.startswith(best[1]) and len(n) - len(best[1]) == 1:
        return None, "marker"          # "cm2a": the PDF keeps the footnote mark as its own word
    return rep, "snapped"


def _pair_leftovers(rows: list[list[str]], inside: list[tuple], unmatched: list[dict]
                    ) -> tuple[list[list[str]], list[dict], list[dict]]:
    """Second pass for tokens too far from any PDF word to snap one at a time (a long
    sequence misread in several places). Pair each still-unmatched token, in row-major
    order, with the leftover PDF words (those no cell carries) in reading order: take the
    best of the next few leftovers, only if it is similar enough and strictly the best."""
    used = {norm(t) for row in rows for cell in row for t in cell.split(" ")}
    ordered = sorted(inside, key=lambda w: (round((w[1] + w[3]) / 2 / 3), w[0]))
    left = [w[4] for w in ordered if len(norm(w[4])) >= MIN_LEN and norm(w[4]) not in used]
    rows = [list(r) for r in rows]
    paired: list[dict] = []
    rest: list[dict] = []
    ptr = 0
    # A model may split one long printed token in two ("ACGCTCCAA GATCCTACCA" for one
    # 19-letter sequence). Consecutive unmatched tokens of a cell are tried joined first.
    groups: list[list[dict]] = []
    for u in unmatched:
        if groups and groups[-1][-1]["row"] == u["row"] and groups[-1][-1]["col"] == u["col"]:
            groups[-1].append(u)
        else:
            groups.append([u])

    def best(token: str, start: int):
        n = norm(token)
        if sum(c.isdigit() for c in n) > MAX_DIGIT_SHARE * len(n):
            return None
        cap = int(PAIR_REL * len(n))
        # A printed cell can be stored as two PDF words ("...GTTTC" + "-TAMRA"), so each
        # leftover is also tried joined to the one after it. Title or footnote words
        # before the cells are skipped because they are nowhere near similar.
        cands = []
        for j in range(start, len(left)):
            for width in (1, 2):
                if j + width > len(left):
                    continue
                text = "".join(left[j:j + width])
                if abs(len(norm(text)) - len(n)) <= cap:
                    cands.append((edit_distance(n, norm(text), cap), j, width, text))
        scored = sorted([c for c in cands if c[0] <= cap][:2 * PAIR_WINDOW])
        if not scored or (len(scored) > 1 and scored[1][0] == scored[0][0]):
            return None
        return scored[0]

    def replace(group: list[dict], hit, tokens: list[str]) -> None:
        nonlocal ptr
        _, j, width, text = hit
        toks = rows[group[0]["row"]][group[0]["col"]].split(" ")
        i = toks.index(tokens[0])
        toks[i:i + len(tokens)] = [text]
        rows[group[0]["row"]][group[0]["col"]] = " ".join(toks)
        paired.append({"row": group[0]["row"], "col": group[0]["col"],
                       "from": " ".join(tokens), "to": text, "edits": hit[0]})
        ptr = j + width

    for g in groups:
        if len(g) > 1:
            hit = best("".join(u["token"] for u in g), ptr)
            if hit is not None:
                replace(g, hit, [u["token"] for u in g])
                continue
        for u in g:
            hit = best(u["token"], ptr)
            if hit is None:
                rest.append(u)
            else:
                replace([u], hit, [u["token"]])
    return rows, paired, rest


def snap_rows(rows: list[list[str]], words: list[tuple], box: list[float]) -> dict:
    """Returns {"rows", "snaps", "unmatched", "skipped"}. `skipped` is a reason when no
    snapping was attempted (no usable text layer, or rotated text)."""
    inside = [w for w in words
              if box[0] <= (w[0] + w[2]) / 2 <= box[2] and box[1] <= (w[1] + w[3]) / 2 <= box[3]]
    if len(inside) < 6:
        return {"rows": rows, "snaps": [], "paired": [], "unmatched": [], "skipped": "no text layer"}
    native = _native(words, box)
    joins = _joins(words, box)
    snaps: list[dict] = []
    unmatched: list[dict] = []
    out: list[list[str]] = []
    for r, row in enumerate(rows):
        new_row = []
        for c, cell in enumerate(row):
            toks = cell.split(" ")
            for k, tok in enumerate(toks):
                rep, why = snap_token(tok, native, joins)
                if rep is not None and rep != tok:
                    snaps.append({"row": r, "col": c, "from": tok, "to": rep})
                    toks[k] = rep
                elif why in {"unmatched", "ambiguous"}:
                    unmatched.append({"row": r, "col": c, "token": tok, "why": why})
            new_row.append(" ".join(toks))
        out.append(new_row)
    out, paired, unmatched = _pair_leftovers(out, inside, [u for u in unmatched
                                                           if u["why"] == "unmatched"]) \
        if any(u["why"] == "unmatched" for u in unmatched) else (out, [], unmatched)
    return {"rows": out, "snaps": snaps, "paired": paired, "unmatched": unmatched,
            "skipped": ""}
