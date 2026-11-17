"""Thin, typed wrappers around the vendored `paper_bundle.py` CLI.

The CLI is the contract. Nothing else in this package writes into the review
directory's build output, and no tool exposed to a model takes a raw path --
the agent names pages and item ids, never files.

Each subcommand prints a JSON result to stdout; we parse and return it.
`verify --strict` exits 2 on a pending state, which is the loop's termination
signal, so a non-zero exit is data here, not an error.
"""
from __future__ import annotations

import contextlib
import json
import re
import unicodedata
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, VENDOR

SCRIPT = VENDOR / "paper_bundle.py"

PENDING = {"mechanical_failure", "unreviewed", "unresolved_discrepancies"}
DONE = {"reviewed", "reviewed_with_limitations"}
# engine commands that fingerprint or read bundle.json after a build
NOTES_HIDDEN_FROM = {"build", "verify", "review-aid"}


@dataclass
class CmdResult:
    ok: bool                 # exit code 0
    code: int
    data: dict               # parsed stdout JSON ({} if unparseable)
    stdout: str
    stderr: str
    seconds: float
    log: Path | None = None

    @property
    def status(self) -> str:
        return str(self.data.get("status", ""))

    @property
    def warnings(self) -> str:
        """stderr when the command otherwise succeeded -- e.g. a missing optional
        dependency. Informational, never a failure."""
        return self.stderr.strip() if (self.ok or self.status) else ""

    @property
    def error(self) -> str:
        """The CLI's own message when it genuinely failed.

        Exit 2 with a parsed status is a pending verification STATE, not a failure:
        the command ran and reported. Only a run that produced no status (exit 1)
        is an error, so stderr warnings are not mistaken for one.
        """
        if self.ok or self.status:
            return ""
        for line in reversed(self.stderr.strip().splitlines()):
            if line.strip():
                return line.strip()
        return f"exit {self.code} with no stderr"


# requirements.txt pins the vendored scripts' dependencies, so the interpreter
# running this package can usually execute them directly.
_VENDOR_DEPS = ("pymupdf", "pymupdf4llm", "pypdf", "PIL")


def _deps_importable() -> bool:
    from importlib.util import find_spec
    try:
        return all(find_spec(m) is not None for m in _VENDOR_DEPS)
    except (ImportError, ValueError):
        return False


def _runner() -> list[str]:
    """How to invoke the vendored CLI.

    Prefers this interpreter when the dependencies are present. `uv run` resolves
    the scripts' inline PEP-723 metadata in a fresh environment each time, which
    can pick a Python with no matching wheels for a transitive dependency and fail
    before the script even starts -- observed with CPython 3.15 and onnxruntime.
    Falling back to uv keeps it working where the deps are not installed.
    """
    if _deps_importable():
        return [sys.executable, str(SCRIPT)]
    if shutil.which("uv"):
        return ["uv", "run", str(SCRIPT)]
    return [sys.executable, str(SCRIPT)]


class Bundle:
    """One review directory."""

    def __init__(self, work: Path, cfg: Config, ledger=None):
        self.work = Path(work)
        self.cfg = cfg
        self.ledger = ledger
        self._n = 0

    def _log(self, name: str, cmd: list[str], proc) -> Path | None:
        """Persist every subcommand's full output.

        The CLI reports a failure by printing `Error: ...` to stderr and
        returning non-zero *without* any JSON on stdout, so a swallowed stderr
        leaves `status=unknown exit=1` and nothing to act on. Never swallow it.
        """
        try:
            d = self.work / "agent-run" / "logs"
            d.mkdir(parents=True, exist_ok=True)
            self._n += 1
            f = d / f"{self._n:02d}-{name}.log"
            f.write_text(f"$ {' '.join(cmd)}\n\nexit: {proc.returncode}\n\n"
                         f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n")
            return f
        except OSError:
            return None

    def _run(self, *args: str) -> CmdResult:
        cmd = _runner() + [a for a in args]
        t0 = time.time()
        if args[0] in NOTES_HIDDEN_FROM:
            with self._notes_withheld():
                proc = subprocess.run(cmd, capture_output=True, text=True)
        else:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        dt = time.time() - t0
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            data = {}
        log = self._log(args[0], cmd, proc)
        if self.ledger is not None:
            self.ledger.record_subcommand(args[0], dt, proc.returncode == 0)
        return CmdResult(proc.returncode == 0, proc.returncode, data,
                         proc.stdout, proc.stderr, dt, log)

    # --- subcommands --------------------------------------------------------
    def prepare(self, inputs: list[str | Path], name: str,
                title: str | None = None, main: str | Path | None = None,
                attachments: list[str | Path] | None = None) -> CmdResult:
        a = ["prepare", *[str(i) for i in inputs], "--work", str(self.work), "--name", name]
        if title:
            a += ["--title", title]
        if main:
            a += ["--main", str(main)]
        for att in (attachments or []):
            a += ["--attachment", str(att)]
        return self._run(*a)

    def extract(self, ocr: str = "auto") -> CmdResult:
        return self._run("extract", "--work", str(self.work), "--ocr", ocr,
                         "--preview-dpi", str(self.cfg.preview_dpi))

    def review_aid(self) -> CmdResult:
        return self._run("review-aid", "--work", str(self.work))

    def reuse_review(self, from_work: Path) -> CmdResult:
        return self._run("reuse-review", "--work", str(self.work),
                         "--from-work", str(from_work))

    @contextlib.contextmanager
    def _notes_withheld(self):
        """Run an engine command with bundle.json `notes` emptied, so the package has no
        "Conversion notes" section; the notes are put back afterwards because they are
        still the working store (see notes.py).

        build, verify and review-aid all see the same emptied file: the engine fingerprints
        bundle.json at build time, and verify fails ("Review input changed after build")
        if the file differs from what was built."""
        path = self.work / "bundle.json"
        if self.cfg.notes_in_package or not path.exists():
            yield
            return
        original = path.read_text()
        data = json.loads(original)
        if not data.get("notes"):
            yield
            return
        stripped = json.dumps({**data, "notes": []}, indent=2, ensure_ascii=False)
        path.write_text(stripped)
        try:
            yield
        finally:
            now = path.read_text()
            if now == stripped:
                path.write_text(original)
            else:      # the command rewrote bundle.json itself: keep its changes, add notes back
                path.write_text(json.dumps({**json.loads(now), "notes": data["notes"]},
                                           indent=2, ensure_ascii=False))

    def build(self, output: Path, require_reviewed: bool | None = None) -> CmdResult:
        return self._build(output, require_reviewed)

    def _build(self, output: Path, require_reviewed: bool | None = None) -> CmdResult:
        prune_navigation(self)          # the list is written before the repairs change headings
        gate = "--require-reviewed" if (
            self.cfg.require_reviewed if require_reviewed is None else require_reviewed
        ) else "--draft"
        return self._run("build", "--work", str(self.work), "--output", str(output),
                         "--dpi", str(self.cfg.asset_dpi),
                         "--max-image-side", str(self.cfg.max_image_side),
                         "--jpeg-quality", str(self.cfg.jpeg_quality), gate)

    def verify(self, strict: bool = True) -> CmdResult:
        a = ["verify", "--work", str(self.work)]
        if strict:
            a.append("--strict")
        return self._run(*a)

    # --- review-directory reads --------------------------------------------
    @property
    def plan_dirs(self) -> list[Path]:
        root = self.work / "documents"
        return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []

    def bundle_json(self) -> dict:
        p = self.work / "bundle.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def write_bundle_json(self, data: dict) -> None:
        (self.work / "bundle.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def inventory(self) -> dict:
        p = self.work / "inventory.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def review_queue(self) -> dict:
        p = self.work / "review-aid" / "review-queue.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def pages(self, source_id: str) -> list[Path]:
        return sorted((self.work / "documents" / source_id / "pages").glob("page-*.json"))

    def page_path(self, source_id: str, page: int) -> Path:
        return self.work / "documents" / source_id / "pages" / f"page-{page:04d}.json"

    def evidence_path(self, source_id: str, page: int) -> Path:
        return self.work / "documents" / source_id / "evidence" / f"page-{page:04d}.json"

    def preview_path(self, source_id: str, page: int) -> Path:
        d = self.work / "documents" / source_id
        for cand in (d / "previews" / f"page-{page:04d}.png",
                     d / "previews" / f"page-{page:04d}.jpg"):
            if cand.exists():
                return cand
        return d / "previews" / f"page-{page:04d}.png"

    def source_pdf(self, source_id: str) -> Path | None:
        d = self.work / "documents" / source_id
        for pat in ("source.pdf", "*.pdf"):
            hits = sorted(d.glob(pat))
            if hits:
                return hits[0]
        return None

    def doc_verification(self, source_id: str) -> dict:
        """documents/<sid>/verification.json -- where the real diagnostic detail is.

        The review queue reports only a check name and a fingerprint (detail is
        null), so it cannot be adjudicated from. This report carries the actual
        differing numbers and missing lines per page.
        """
        p = self.work / "documents" / source_id / "verification.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def adjudications_path(self, source_id: str) -> Path:
        return self.work / "documents" / source_id / "adjudications.json"


# Publisher font encodings mangle comparison symbols in extracted text. Measured on
# an Elsevier PDF: ">" extracts as "N" and "<" as "b". The correct review restores
# the symbol. Deleting it instead is silently destructive -- "2.4-N3.9-log10" is
# "2.4 to >3.9", and dropping the symbol turns an open bound into a point value.
# Verification cannot catch this: the raw line is reported missing either way.
MANGLED = {"N": ">", "b": "<"}
# A mangled symbol sits before a number, sometimes with a space: "N99%", "P N 0.63".
# The number that follows is the anchor used to find it again in the built text.
_MANGLE_RE = re.compile(r"(?<![0-9A-Za-z])([Nb])\s{0,2}([0-9][0-9.,]*)")


def _norm(text: str) -> str:
    """Collapse whitespace and unify dash variants so lines can be compared."""
    return re.sub(r"\s+", " ", text.replace("\u2013", "-").replace("\u2014", "-")
                  .replace("\u2212", "-")).strip()


def _rows_text(rows: list[list[str]]) -> str:
    return "\n".join(" | ".join(str(c) for c in r) for r in rows) + "\n"


def _glyph_key(text: str) -> str:
    """Alphanumerics with the glyphs fonts routinely lose or swap removed: control
    characters, the degree sign, and mu (extracted as `m`) all collapse."""
    t = re.sub(r"[\x00-\x1f\ufffd\u00b0]", "", text.replace("\u00b5", "m").replace("\u03bc", "m"))
    return re.sub(r"[\W_]+", "", t).lower()


def classify_missing_line(raw: str, built_text: str) -> str:
    """Why is this raw extracted line absent from the built output?

    Compares by the NUMBER the symbol qualifies, not by surrounding text. A review
    legitimately changes other characters on the same line -- `log10` becomes
    `log\u2081\u2080`, `FeCl3` becomes `FeCl\u2083` -- so any literal window comparison
    produces false alarms. The number is stable; what matters is whether a `>` or
    `<` now sits in front of it.

    Returns:
      "restored"        the number is present with > or < in front of it. A correct
                        repair; the diagnostic is an artifact of having made it;
      "symbol_dropped"  the number is present with NO symbol in front. A defect:
                        an open bound silently became a closed one;
      "dash_encoded"    the raw line spells an en dash as `e` ("290e470"); the dash is
                        in the output. A correct repair, not a loss;
      "glyph_encoded"   the raw line holds a control character where a degree/micro sign
                        was lost, and the line is present once glyphs are ignored;
      "other"           the line survives verbatim, or differs for another reason;
      "absent"          the number is not in the output at all.
    """
    nraw, nbuilt = _norm(raw), _norm(built_text)
    # Unchanged line: nothing was dropped. Checked first because `N`/`b` before a
    # digit occur legitimately -- the address "N13W8", a label like "sample N3".
    if nraw and nraw in nbuilt:
        return "other"
    # This publisher family also writes an en dash as the letter `e` between digits
    # ("290e470") or words ("AugusteNovember"). If the line is present once that is
    # undone, the review corrected it and the diagnostic is an artifact of the fix.
    undashed = _norm(re.sub(r"(?<=[a-z])e(?=[A-Z])", "-", re.sub(r"(?<=\d)e(?=\d)", "-", raw)))
    if undashed != nraw:
        alnum = lambda t: re.sub(r"[\W_]+", "", t).lower()  # noqa: E731
        if alnum(undashed) and alnum(undashed) in alnum(nbuilt):
            return "dash_encoded"
    if re.search(r"[\x00-\x1f]", raw) and len(_glyph_key(raw)) >= 12 \
            and _glyph_key(raw) in _glyph_key(nbuilt):
        return "glyph_encoded"
    matches = list(_MANGLE_RE.finditer(nraw))
    if not matches:
        head = nraw[:40]
        return "other" if head and head in nbuilt else "absent"

    verdicts: list[str] = []
    for m in matches:
        num = m.group(2).rstrip(".,")
        found = False
        for occ in re.finditer(re.escape(num), nbuilt):
            # look at the few non-space characters immediately before the number
            before = nbuilt[max(0, occ.start() - 4):occ.start()].replace(" ", "")
            found = True
            if ">" in before or "<" in before:
                verdicts.append("restored")
                break
        else:
            verdicts.append("symbol_dropped" if found else "absent")
    # Worst verdict wins: one dropped symbol is a defect regardless of the rest.
    for worst in ("symbol_dropped", "absent", "restored"):
        if worst in verdicts:
            return worst
    return "other"


PAPER_FILE, SUPPLEMENT_FILE = "references/paper.md", "references/supplement.md"
NAV_MAX_ENTRIES = 40


def document_headings(b: "Bundle") -> dict[str, list[dict]]:
    """Every heading the built documents will contain, per output file.

    `index_text` validates a curated `navigation` entry by looking for its heading
    in the BUILT markdown, and a heading that is not found verbatim stops the
    build. Synthesis runs before the build, so the candidates are derived from the
    page plans -- a heading item's markdown `## 1. Introduction` becomes the
    heading text `1. Introduction`.
    """
    out: dict[str, list[dict]] = {PAPER_FILE: [], SUPPLEMENT_FILE: []}
    roles = {s.get("id"): s.get("role") for s in b.bundle_json().get("sources", [])}
    for d in b.plan_dirs:
        target = SUPPLEMENT_FILE if roles.get(d.name) == "supplement" else PAPER_FILE
        for path in b.pages(d.name):
            try:
                plan = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for it in plan.get("items", []):
                if it.get("kind") != "heading":
                    continue
                md = (it.get("markdown") or "").strip()
                # the builder reads a heading line by line, so a title wrapped over two
                # lines is known to it by its first line only
                md = md.split("\n", 1)[0].strip()
                m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", md)
                level, text = (len(m[1]), m[2]) if m else (0, md)
                if text:
                    out[target].append({"file": target, "level": level, "heading": text,
                                        "page": plan.get("page")})
    return out


def validate_navigation(entries: list[dict],
                        headings: dict[str, list[dict]]) -> list[str]:
    """Problems that would make the build reject this navigation list.

    Mirrors `reading_package.index_text`: the file must be one of the two output
    documents, the heading must be present verbatim, and the list has size caps.
    One bad entry fails the whole build, so this runs before anything is written.
    """
    problems: list[str] = []
    known = {f: {h["heading"] for h in hs} for f, hs in headings.items()}
    seen: set[tuple[str, str]] = set()
    for e in entries:
        f, h = e.get("file"), (e.get("heading") or "").strip()
        if f not in (PAPER_FILE, SUPPLEMENT_FILE):
            problems.append(f"file must be {PAPER_FILE} or {SUPPLEMENT_FILE}, got {f!r}")
            continue
        if not h:
            problems.append("entry has an empty heading")
            continue
        if h not in known.get(f, set()):
            near = [k for k in known.get(f, set()) if k[:18] == h[:18]]
            problems.append(f"heading {h[:60]!r} is not in {f}"
                            + (f"; closest is {near[0][:60]!r}" if near else ""))
        if (f, h) in seen:
            problems.append(f"duplicate entry for {h[:50]!r}")
        seen.add((f, h))
    if len(entries) > NAV_MAX_ENTRIES:
        problems.append(f"{len(entries)} entries exceeds the {NAV_MAX_ENTRIES} cap")
    size = sum(len(str(e.get("heading", ""))) + len(str(e.get("purpose", ""))) + 8
               for e in entries)
    if size > 12000:
        problems.append(f"navigation is ~{size} characters; the builder caps the "
                        "rendered index at 14000")
    return problems


def prune_navigation(b: "Bundle") -> int:
    """Drop curated navigation entries whose heading is no longer in the documents.

    Synthesis writes the list before the repair passes run, and those passes demote,
    re-level or reword headings. The builder fails the whole build on one entry it
    cannot find, so an index with fewer rows beats no package. Returns entries dropped.
    """
    data = b.bundle_json()
    nav = data.get("navigation")
    if not nav:
        return 0
    known = {f: {h["heading"] for h in hs} for f, hs in document_headings(b).items()}
    # The builder leaves out a heading near the top of page 1 that merely extends the
    # document title it generates ("Title: subtitle"), so it is not in paper.md.
    def canon(t: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", re.sub(r"[#*_`]", "", t))
                       if c.isalnum()).casefold()
    wanted = canon(data.get("title") or "")
    if wanted:
        for d in b.plan_dirs:
            first = next(iter(b.pages(d.name)), None)
            try:
                items = json.loads(first.read_text()).get("items", []) if first else []
            except (OSError, json.JSONDecodeError):
                continue
            for it in items:
                if it.get("kind") != "heading" or it["bbox"][1] > 130:
                    continue
                actual = canon(it.get("markdown") or "")
                if wanted in actual and actual != wanted:
                    first_line = (it.get("markdown") or "").split("\n", 1)[0]
                    text = re.sub(r"^#{1,6}\s+", "", first_line).strip()
                    for f in known:
                        known[f].discard(text)
    kept, seen = [], set()
    for e in nav:
        key = (e.get("file"), (e.get("heading") or "").strip())
        if key[1] in known.get(key[0], set()) and key not in seen:
            kept.append(e)
            seen.add(key)
    if len(kept) != len(nav):
        data["navigation"] = kept
        b.write_bundle_json(data)
    return len(nav) - len(kept)


def unreviewed_pages(b: "Bundle") -> list[tuple[str, int]]:
    """Pages whose plan is not marked reviewed, across every document.

    `build --require-reviewed` refuses outright if any page is pending, so this is
    checked before a build rather than spending one to be told.
    """
    out: list[tuple[str, int]] = []
    for d in b.plan_dirs:
        for p in b.pages(d.name):
            try:
                page = int(p.stem.split("-")[-1])
            except ValueError:
                continue
            try:
                if not json.loads(p.read_text()).get("reviewed"):
                    out.append((d.name, page))
            except (OSError, json.JSONDecodeError):
                out.append((d.name, page))      # unreadable counts as pending
    return out


def check_symbol_repairs(b: "Bundle") -> list[dict]:
    """Every missing line across the review, classified. A `symbol_dropped` entry is
    a real data defect and must not be adjudicated away."""
    out: list[dict] = []
    for d in b.plan_dirs:
        sid = d.name
        rep = b.doc_verification(sid)
        for pg in (rep.get("pages") or []):
            missing = pg.get("missing_lines") or []
            if not missing:
                continue
            page = int(pg.get("page", 0))
            try:
                plan = json.loads(b.page_path(sid, page).read_text())
            except (OSError, json.JSONDecodeError):
                continue
            built = " ".join((i.get("markdown") or "") for i in plan.get("items", []))
            for line in missing:
                raw = line.get("text", "") if isinstance(line, dict) else str(line)
                out.append({"source_id": sid, "page": page, "raw": raw,
                            "verdict": classify_missing_line(raw, built)})
    return out


def _tolerant_pattern(text: str) -> str:
    """Regex for `text` that tolerates whitespace and dash-variant differences.

    The raw extracted line and the built markdown use different dash characters
    and spacing for the same content, so a literal search misses.
    """
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            out.append(r"\s*")
        elif ch in "-\u2013\u2014\u2212":
            out.append(r"[-\u2013\u2014\u2212]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def repair_dropped_symbols(b: "Bundle", apply: bool = True) -> list[dict]:
    """Put back comparison symbols that a review deleted instead of restoring.

    This is deliberately deterministic. The correct answer is not a judgement
    call: the raw extraction records a mangled `N` or `b` at a known position
    before a known number, which means the glyph on the page is `>` or `<`. Two
    agent passes were given the rule explicitly and still left the symbols out,
    so the repair is done mechanically from the evidence instead.

    The edit goes through PageEditor, so the integrity rules and the operation log
    apply exactly as they do for a model's edit.

    Pass apply=False to report what would change without writing.
    """
    from .editor import EditError, PageEditor

    results: list[dict] = []
    dropped = [d for d in check_symbol_repairs(b) if d["verdict"] == "symbol_dropped"]
    by_page: dict[tuple[str, int], list[str]] = {}
    for d in dropped:
        by_page.setdefault((d["source_id"], d["page"]), []).append(d["raw"])

    for (sid, page), raws in by_page.items():
        try:
            ed = PageEditor(b.page_path(sid, page))
        except (OSError, ValueError) as exc:
            results.append({"source_id": sid, "page": page, "applied": False,
                            "detail": f"could not open page plan: {exc}"})
            continue
        changed = False
        for raw in raws:
            for m in _MANGLE_RE.finditer(_norm(raw)):
                sym, num = MANGLED[m.group(1)], m.group(2).rstrip(".,")
                nraw = _norm(raw)
                # anchor on the few characters before the symbol so the number is
                # identified unambiguously on the page
                prefix = nraw[max(0, m.start() - 8):m.start()].strip()
                if not prefix:
                    results.append({"source_id": sid, "page": page, "applied": False,
                                    "detail": f"no anchor text before {sym}{num}"})
                    continue
                pat = re.compile(_tolerant_pattern(prefix) + r"\s*" + re.escape(num))
                hit = None
                for it in ed.items:
                    md = it.get("markdown") or ""
                    found = list(pat.finditer(md))
                    if len(found) == 1:
                        hit = (it, found[0])
                        break
                    if len(found) > 1:
                        results.append({"source_id": sid, "page": page, "applied": False,
                                        "detail": f"{prefix!r}+{num} is ambiguous in "
                                                  f"{it['id']} ({len(found)} matches)"})
                        hit = "ambiguous"
                        break
                if hit is None or hit == "ambiguous":
                    if hit is None:
                        results.append({"source_id": sid, "page": page, "applied": False,
                                        "detail": f"could not locate {prefix!r} before {num}"})
                    continue
                it, mm = hit
                md = it["markdown"]
                k = mm.end() - len(num)            # insert immediately before the number
                new = md[:k] + sym + md[k:]
                if not apply:
                    results.append({"source_id": sid, "page": page, "applied": False,
                                    "detail": f"would insert {sym!r} before {num} in {it['id']}"})
                    continue
                try:
                    ed.set_markdown(it["id"], new)
                    changed = True
                    results.append({"source_id": sid, "page": page, "applied": True,
                                    "detail": f"inserted {sym!r} before {num} in {it['id']}"})
                except EditError as exc:
                    results.append({"source_id": sid, "page": page, "applied": False,
                                    "detail": f"editor refused: {exc}"})
        if changed and apply:
            notes = (ed.data.get("review_notes") or "").rstrip()
            ed.data["review_notes"] = (
                notes + " Deterministic repair: comparison symbols that the review "
                "deleted rather than restored were put back from the raw extraction "
                "(a mangled 'N' before a number is '>', 'b' is '<').").strip()
            try:
                ed.save()
            except EditError as exc:
                results.append({"source_id": sid, "page": page, "applied": False,
                                "detail": f"save refused: {exc}"})
    return results


_CAPTION_TEXT_RE = re.compile(r"^(?:Fig\.|Figure|Table)\s*S?\d+[.:]\s")
_TERMINAL_RE = re.compile(r"[.?!:][)\]\"'\u201d\u2019]*(?:</sup>)?\s*$")
_FLOATS = {"figure", "table"}


def _ends_sentence(md: str) -> bool:
    return bool(_TERMINAL_RE.search(md or ""))


def _is_prose(it: dict) -> bool:
    """A paragraph, not a spaced-out label like 'G R A P H I C A L  A B S T R A C T'."""
    md = it.get("markdown") or ""
    return it.get("kind") == "text" and len(md) >= 60 and any(c.islower() for c in md)


def _continues_prior(it: dict) -> bool:
    """Prose that picks up a sentence begun earlier. The reviewer is meant to set
    `join_previous`, but a paragraph opening in lower case is just as clear."""
    md = (it.get("markdown") or "").lstrip()
    return it.get("kind") == "text" and bool(it.get("join_previous") or md[:1].islower())


def _short_line(it: dict) -> bool:
    bb = it.get("bbox") or []
    h = (bb[3] - bb[1]) if len(bb) == 4 else 0
    return len(it.get("markdown") or "") <= 200 and h <= 15


def find_float_clusters(items: list[dict]) -> list[list[int]]:
    """Index groups: a figure or table together with its label and caption.

    A table is `Table 1` (kind caption), a title line (kind text), then the table;
    a figure is the figure then its caption, which the extractor may call either
    `caption` or `text`. Prose is never swept in: a text item joins only if it is a
    short single line next to a caption, or opens with `Fig. N.` / `Table N`.
    """
    clusters: list[list[int]] = []
    used: set[int] = set()
    for i, it in enumerate(items):
        if it.get("kind") not in _FLOATS or i in used:
            continue
        lo = hi = i
        j = i - 1
        while j >= 0 and j not in used:
            k = items[j].get("kind")
            md = items[j].get("markdown") or ""
            if k == "caption" or (k == "text" and _CAPTION_TEXT_RE.match(md)):
                lo, j = j, j - 1
            elif (k == "text" and _short_line(items[j]) and j >= 1
                  and items[j - 1].get("kind") == "caption"):
                lo, j = j, j - 1
            else:
                break
        j = i + 1
        while j < len(items):
            k = items[j].get("kind")
            md = items[j].get("markdown") or ""
            if k == "caption" or (k == "text" and _CAPTION_TEXT_RE.match(md)):
                hi, j = j, j + 1
            else:
                break
        group = list(range(lo, hi + 1))
        used.update(group)
        clusters.append(group)
    return clusters


def plan_float_hoist(items: list[dict], cluster: list[int]) -> dict | None:
    """Where one cluster should move so it no longer interrupts a sentence.

    Returns None when it sits at a clean break, {"refuse": why} when it is mid-
    sentence but cannot be placed without guessing, else {"before"|"after": id}.
    """
    ids = {items[i]["id"] for i in cluster}
    prev = next((items[i] for i in range(cluster[0] - 1, -1, -1)
                 if items[i].get("kind") != "omit"), None)
    nxt = next((items[i] for i in range(cluster[-1] + 1, len(items))
                if items[i].get("kind") != "omit"), None)

    if prev is not None and _is_prose(prev) and not _ends_sentence(prev.get("markdown")):
        if prev.get("join_previous"):
            return {"refuse": f"{prev['id']} is unterminated and itself continues "
                              "from before; no clean break to move to"}
        return {"before": prev["id"]}

    if nxt is not None and _continues_prior(nxt):
        n = items.index(nxt)
        for it in items[n:]:
            if it["id"] in ids:
                continue
            if it.get("kind") not in {"text", "omit"}:
                break
            if it.get("kind") == "text" and _ends_sentence(it.get("markdown")):
                return {"after": it["id"]}
        return {"refuse": f"no sentence-ending item found after the continuation {nxt['id']}"}
    return None


_DEGREE_RE = re.compile(r"(?<=\d)(\s*)(?:<sup>)?\ufffd(?:</sup>)?\s*(?=[CF]\b)")


def repair_degree_signs(b: "Bundle", apply: bool = True) -> list[dict]:
    """'25 <sup>\ufffd</sup>C' -> '25 \u00b0C'. A replacement character after a number and
    before C/F is always a degree sign the font encoding lost (paper 17's table)."""
    from .editor import EditError, PageEditor

    results: list[dict] = []
    fix = lambda t: _DEGREE_RE.sub(" \u00b0", t)  # noqa: E731
    for d in b.plan_dirs:
        sid = d.name
        for path in b.pages(sid):
            try:
                ed = PageEditor(path)
                page = int(path.stem.split("-")[-1])
            except (OSError, ValueError):
                continue
            changed = False
            for it in ed.items:
                md = it.get("markdown") or ""
                if it.get("kind") in {"text", "caption", "heading"} and "\ufffd" in md \
                        and fix(md) != md:
                    results.append({"source_id": sid, "page": page, "applied": apply,
                                    "detail": f"{it['id']}: degree sign restored in text"})
                    if apply:
                        it["markdown"] = fix(md)
                        ed.ops.append(f"set_markdown({it['id']}, degree sign)")
                        changed = True
                rows = it.get("rows")
                if it.get("kind") == "table" and rows and any(
                        "\ufffd" in c and fix(c) != c for r in rows for c in r):
                    results.append({"source_id": sid, "page": page, "applied": apply,
                                    "detail": f"{it['id']}: degree sign restored in table cells"})
                    if apply:
                        it["rows"] = [[fix(c) for c in r] for r in rows]
                        ed.ops.append(f"set_rows({it['id']}, degree sign)")
                        changed = True
            if changed:
                notes = (ed.data.get("review_notes") or "").rstrip()
                ed.data["review_notes"] = (notes + " Deterministic repair: a degree sign "
                                           "lost to font encoding was restored.").strip()
                try:
                    ed.save()
                except EditError as exc:
                    results.append({"source_id": sid, "page": page, "applied": False,
                                    "detail": f"save refused: {exc}"})
    return results


def _heading_level(md: str) -> int:
    """'3.7. Title' -> 3 ('###'), '3. Title' -> 2 ('##'), unnumbered -> 2."""
    m = re.match(r"\s*(\d+(?:\.\d+)*)\.?\s", md)
    return min(6, 1 + len(m.group(1).split("."))) if m else 2


def _is_sentence_fragment(markdown: str) -> bool:
    """A 'heading' that is really the middle of a sentence: it starts lower-case and is
    long (paper 15 p4: '## procedure and Oracle Crystal Ball software ... change (as')."""
    body = re.sub(r"^#+\s*", "", markdown).strip()
    return len(body) > 45 and body[:1].islower() and not re.match(r"[a-z]\)|\(", body)


def repair_cross_page_joins(b: "Bundle", apply: bool = True) -> list[dict]:
    """`join_previous` on the first item of a page joins it onto the LAST item of the
    previous page, which the builder only allows for text or a caption. The per-page
    repair cannot see across the boundary (paper 15 p5 joined onto p4's heading)."""
    from .editor import EditError, PageEditor

    results: list[dict] = []
    for d in b.plan_dirs:
        paths = b.pages(d.name)
        last_kind = None
        for path in paths:
            try:
                ed = PageEditor(path)
            except (OSError, ValueError):
                continue
            live = [i for i in ed.items if i.get("kind") != "omit"]
            if live and live[0].get("join_previous") and (
                    live[0].get("kind") not in {"text", "caption"}
                    or last_kind not in {"text", "caption"}):
                page = int(path.stem.split("-")[-1])
                results.append({"source_id": d.name, "page": page, "applied": apply,
                                "detail": f"{live[0]['id']}: cleared join_previous across the "
                                          f"page break (previous page ends with a {last_kind})"})
                if apply:
                    live[0]["join_previous"] = None
                    ed.ops.append(f"set_join_previous({live[0]['id']}, null)")
                    try:
                        ed.save()
                    except EditError as exc:
                        results[-1].update(applied=False, detail=f"save refused: {exc}")
            last_kind = live[-1].get("kind") if live else last_kind
    return results


def repair_builder_constraints(b: "Bundle", apply: bool = True) -> list[dict]:
    """Fix two reviewer slips the builder or navigation then trips over.

    * a heading item whose markdown lost its '#' (paper 8: '3.7. ...' was kept as
      plain text, so the curated navigation could not find it). The level comes
      from the section number, which is the paper's own convention.
    * `join_previous` on an item that follows a heading/figure/table (paper 15: the
      builder refuses it). A paragraph after a heading is a new paragraph, so the
      join is cleared.

    Also: level-1 `#` section headings (the index skips them) and the graphical
    abstract left carrying an extractor id. All have one correct answer, so they are
    mechanical and go through PageEditor.
    """
    from .editor import HEADING_RE, EditError, PageEditor

    results: list[dict] = []
    for d in b.plan_dirs:
        sid = d.name
        seen_assets: set[str] = set()
        for path in b.pages(sid):
            try:
                ed = PageEditor(path)
                page = int(path.stem.split("-")[-1])
            except (OSError, ValueError):
                continue
            changed = False
            # A table or figure that runs onto the next page is reviewed as two items that
            # both got its name ("table-2"); the builder refuses a repeated asset_name.
            # The later piece becomes "<name>-cont" ("-cont2", ...) and keeps its label.
            for it in ed.items:
                if it.get("kind") not in {"figure", "formula", "table"}:
                    continue
                name = it.get("asset_name") or it["id"]
                if name in seen_assets:
                    new, k = f"{name}-cont", 2
                    while new in seen_assets:
                        new, k = f"{name}-cont{k}", k + 1
                    results.append({"source_id": sid, "page": page, "applied": apply,
                                    "detail": f"{it['id']}: duplicate asset_name {name!r} "
                                              f"renamed {new!r}"})
                    if apply:
                        it["asset_name"] = new
                        ed.ops.append(f"set_asset({it['id']}, {new})")
                        changed = True
                        name = new
                seen_assets.add(name)
            for it in ed.items:
                md = it.get("markdown") or ""
                if it.get("kind") == "heading" and not HEADING_RE.match(md):
                    new = "#" * _heading_level(md) + " " + md.strip()
                    results.append({"source_id": sid, "page": page, "applied": apply,
                                    "detail": f"{it['id']}: heading gained '#' ({new[:40]!r})"})
                    if apply:
                        it["markdown"] = new
                        ed.ops.append(f"set_markdown({it['id']}, heading marker)")
                        changed = True
            for it in ed.items:
                md = it.get("markdown") or ""
                m = re.match(r"^#\s+(.*)$", md)
                if it.get("kind") in {"heading", "text"} and m:
                    # the builder owns '#' for the document title; a section written as
                    # '#' is skipped by the navigation index
                    body = m.group(1)
                    if re.match(r"\s*\d+(?:\.\d+)*\.?\s", body):
                        new = "#" * _heading_level(body) + " " + body
                    elif page == 1:
                        new = None            # the paper's own title: plain text
                    else:
                        new = "## " + body
                    results.append({"source_id": sid, "page": page, "applied": apply,
                                    "detail": f"{it['id']}: level-1 heading -> "
                                              f"{'plain text' if new is None else new[:30]!r}"})
                    if apply:
                        if new is None:
                            it["kind"], it["markdown"] = "text", body
                        elif it["kind"] == "text":
                            it["kind"], it["markdown"] = "heading", new
                        else:
                            it["markdown"] = new
                        ed.ops.append(f"set_markdown({it['id']}, heading level)")
                        changed = True
            for n, it in enumerate(ed.items):
                if it.get("kind") in {"figure"} and not it.get("label_confirmed") \
                        and re.match(r"^figure-p\d{4}-\d{3}$", it.get("asset_name") or ""):
                    found = False
                    for x in reversed(ed.items[:n]):
                        if x.get("kind") == "heading":
                            break
                        if x.get("kind") == "text" and re.sub(
                                r"\s+", "", x.get("markdown") or "").lower() == "graphicalabstract":
                            found = True
                            break
                    if found:
                        results.append({"source_id": sid, "page": page, "applied": apply,
                                        "detail": f"{it['id']}: named graphical-abstract"})
                        if apply:
                            it["asset_name"], it["label"] = "graphical-abstract", "Graphical abstract"
                            ed.ops.append(f"set_asset({it['id']}, graphical-abstract)")
                            changed = True
            for it in ed.items:
                if it.get("kind") == "heading" and _is_sentence_fragment(it.get("markdown") or ""):
                    results.append({"source_id": sid, "page": page, "applied": apply,
                                    "detail": f"{it['id']}: mid-sentence text was marked as a "
                                              "heading; demoted to text"})
                    if apply:
                        it["kind"] = "text"
                        it["markdown"] = re.sub(r"^#+\s*", "", it.get("markdown") or "")
                        ed.ops.append(f"set_kind({it['id']}, text; sentence fragment)")
                        changed = True
            live = [i for i in ed.items if i.get("kind") != "omit"]
            for n, it in enumerate(live):
                if it.get("join_previous") and n and (
                        it.get("kind") not in {"text", "caption"}
                        or live[n - 1].get("kind") not in {"text", "caption"}):
                    results.append({"source_id": sid, "page": page, "applied": apply,
                                    "detail": f"{it['id']}: cleared join_previous after "
                                              f"a {live[n - 1].get('kind')} item"})
                    if apply:
                        it["join_previous"] = None
                        ed.ops.append(f"set_join_previous({it['id']}, null)")
                        changed = True
            if changed:
                notes = (ed.data.get("review_notes") or "").rstrip()
                ed.data["review_notes"] = (notes + " Deterministic repair: a heading marker "
                                           "or an invalid paragraph join the builder "
                                           "rejects was corrected.").strip()
                try:
                    ed.save()
                except EditError as exc:
                    results.append({"source_id": sid, "page": page, "applied": False,
                                    "detail": f"save refused: {exc}"})
    return results


def _join_violations(items: list[dict]) -> set[str]:
    """Ids whose `join_previous` the builder would refuse: it needs the previous
    (non-omitted) item to be text or caption. Mirrors vendor/reading_package.py."""
    live = [it for it in items if it.get("kind") != "omit"]
    bad = set()
    for n, it in enumerate(live):
        if it.get("join_previous") and n and (
                it.get("kind") not in {"text", "caption"}
                or live[n - 1].get("kind") not in {"text", "caption"}):
            bad.add(it["id"])
    return bad


def _moved(items: list[dict], ids: list[str], where: str, anchor: str) -> list[dict]:
    group = [it for it in items if it["id"] in set(ids)]
    rest = [it for it in items if it["id"] not in set(ids)]
    k = next(n for n, it in enumerate(rest) if it["id"] == anchor)
    k = k if where == "before" else k + 1
    return rest[:k] + group + rest[k:]


def _safe_move(items: list[dict], ids: list[str], where: str, anchor: str) -> bool:
    """A move may not create a join the builder refuses, nor turn a page that ends
    in prose into one that ends in a bare figure/table (the next page may join it)."""
    after = _moved(items, ids, where, anchor)
    if _join_violations(after) - _join_violations(items):
        return False
    live = lambda xs: [x for x in xs if x.get("kind") != "omit"]  # noqa: E731
    was, now = live(items)[-1:], live(after)[-1:]
    if was and now and was[0].get("kind") in {"text", "caption"} \
            and now[0].get("kind") not in {"text", "caption"}:
        return False
    return True


def hoist_floats(b: "Bundle", apply: bool = True) -> list[dict]:
    """Move figures and tables that interrupt a sentence to the nearest clean break.

    Verification ignores order, so a float dropped between "...and the residual"
    and "aluminum concentration" passes every check and still reads wrongly. The
    right place is not a judgement call, so this is mechanical, goes through
    PageEditor, and is idempotent. A model is only asked to do it first.
    """
    from .editor import EditError, PageEditor

    results: list[dict] = []
    for d in b.plan_dirs:
        sid = d.name
        for path in b.pages(sid):
            try:
                ed = PageEditor(path)
                page = int(path.stem.split("-")[-1])
            except (OSError, ValueError):
                continue
            changed = False
            done: set[str] = set()      # a group moves at most once per run, so two
            for _ in range(len(ed.items)):   # groups can never trade places forever
                moved = False
                for cl in find_float_clusters(ed.items):
                    ids = [ed.items[i]["id"] for i in cl]
                    if ids[0] in done:
                        continue
                    plan = plan_float_hoist(ed.items, cl)
                    if plan is None:
                        continue
                    if "refuse" in plan:
                        rec = {"source_id": sid, "page": page, "applied": False,
                               "detail": f"{'+'.join(ids)}: {plan['refuse']}"}
                        if rec not in results:
                            results.append(rec)
                        continue
                    where = "before" if "before" in plan else "after"
                    if not _safe_move(ed.items, ids, where, plan[where]):
                        results.append({"source_id": sid, "page": page, "applied": False,
                                        "detail": f"{'+'.join(ids)}: moving it {where} "
                                                  f"{plan[where]} would break a join_previous"})
                        done.add(ids[0])
                        continue
                    if not apply:
                        results.append({"source_id": sid, "page": page, "applied": False,
                                        "detail": f"would move {'+'.join(ids)} {where} "
                                                  f"{plan[where]}"})
                        continue
                    try:
                        ed.hoist(ids, **{where: plan[where]})
                    except EditError as exc:
                        results.append({"source_id": sid, "page": page, "applied": False,
                                        "detail": f"editor refused: {exc}"})
                        continue
                    changed = moved = True
                    done.add(ids[0])
                    results.append({"source_id": sid, "page": page, "applied": True,
                                    "detail": f"moved {'+'.join(ids)} {where} {plan[where]}"})
                    break
                if not moved:
                    break
            if changed:
                notes = (ed.data.get("review_notes") or "").rstrip()
                ed.data["review_notes"] = (
                    notes + " Deterministic repair: a figure or table that interrupted "
                    "a sentence was moved to the nearest paragraph break; no content "
                    "was changed.").strip()
                try:
                    ed.save()
                except EditError as exc:
                    results.append({"source_id": sid, "page": page, "applied": False,
                                    "detail": f"save refused: {exc}"})
    return results


def collect_diagnostics(b: "Bundle") -> list[dict]:
    """Join the queue's fingerprinted entries to the verification report's detail.

    Two sources, both needed:
      * review-queue.json  -> the exact `adjudication_entry` (bound to a fingerprint
        of the diagnostic, so it must be copied verbatim), but `detail` is null.
      * documents/<sid>/verification.json -> the actual differing number tokens and
        missing lines, per page.

    Without the second, an adjudicator is judging a check name and a hash and can
    only ever say "repair needed", which never converges.
    """
    out: list[dict] = []
    queue = b.review_queue()
    if not isinstance(queue, dict):
        return out
    for src in (queue.get("sources") or []):
        sid = src.get("id") or ""
        rep = b.doc_verification(sid)
        detail_by_page = {int(p.get("page", 0)): p for p in (rep.get("pages") or [])}
        plans: dict[int, str] = {}
        for it in (src.get("items") or []):
            entry = it.get("adjudication_entry")
            if not entry:
                continue
            page = int(it.get("page") or entry.get("page") or 0)
            check = entry.get("check") or it.get("kind") or ""
            pd = detail_by_page.get(page, {})
            nd = pd.get(check) if isinstance(pd.get(check), dict) else {}
            if page not in plans:
                # The classifier needs the WHOLE page: clipping each item to 900
                # characters hid a sentence at offset 1049 of a 1061-character item
                # and produced a false "absent" (paper 8). Only the prompt copy is
                # clipped, and generously.
                try:
                    plan = json.loads(b.page_path(sid, page).read_text())
                    plans[page] = "\n\n".join(
                        f"[{i['id']} {i.get('kind')}] {i.get('rows') and _rows_text(i['rows']) or ''}"
                        f"{i.get('markdown') or ''}"
                        for i in plan.get("items", []))
                except (OSError, json.JSONDecodeError):
                    plans[page] = "(page plan unavailable)"
            classified = [
                {"raw": (ln.get("text", "") if isinstance(ln, dict) else str(ln)),
                 "verdict": classify_missing_line(
                     ln.get("text", "") if isinstance(ln, dict) else str(ln),
                     plans[page])}
                for ln in (pd.get("missing_lines") or [])[:8]]
            out.append({
                "source_id": sid, "page": page, "check": check,
                "missing_lines_classified": classified,
                "numbers_in_source_not_in_output": nd.get("missing") or {},
                "numbers_in_output_not_in_source": nd.get("extra") or {},
                "missing_lines": (pd.get("missing_lines") or [])[:8],
                "built_page_text": plans[page][:40000],
                "adjudication_entry": entry,
            })
    return out


# `pdf_to_skill.load_adjudications` requires exactly this shape and rejects the
# file otherwise -- writing a bare list makes the build die on `data.get(...)`.
ADJ_SCHEMA_VERSION = 1
ADJ_CHECKS = {"missing_lines", "number_differences",
              "independent_parser_number_differences"}


def valid_adjudication(entry: dict) -> str:
    """Why this entry would be rejected by the engine, or '' if it is valid.

    Mirrors the engine's own validation so a bad entry is reported here instead of
    failing the build with a parse error that names no page.
    """
    page, check = entry.get("page"), entry.get("check")
    fp, reason = entry.get("fingerprint"), entry.get("reason")
    if not isinstance(page, int) or page < 1:
        return f"page must be a positive int, got {page!r}"
    if check not in ADJ_CHECKS:
        return f"check must be one of {sorted(ADJ_CHECKS)}, got {check!r}"
    if not isinstance(fp, str) or not re.fullmatch(r"[0-9a-f]{64}", fp):
        return "fingerprint must be 64 lowercase hex characters"
    if not isinstance(reason, str) or not reason.strip():
        return "reason must be a non-empty string"
    if reason.strip() == "Describe the visible source check.":
        return "reason is still the queue's placeholder"
    return ""


def prune_stale_adjudications(b: "Bundle") -> int:
    """Remove adjudications the engine reports as stale.

    A page re-review changes the page text, so the fingerprint of an earlier
    adjudication no longer matches any diagnostic. The engine then reports the
    package `unresolved_discrepancies` because of the leftover entry alone, even
    with nothing to adjudicate (paper 8). A stale entry describes a diagnostic that
    no longer exists, so dropping it loses nothing.
    """
    removed = 0
    for d in b.plan_dirs:
        stale = {(e.get("page"), e.get("check"), e.get("fingerprint"))
                 for e in (b.doc_verification(d.name).get("stale_adjudications") or [])}
        path = b.adjudications_path(d.name)
        if not stale or not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        entries = data.get("entries") if isinstance(data, dict) else None
        if not entries:
            continue
        keep = [e for e in entries
                if (e.get("page"), e.get("check"), e.get("fingerprint")) not in stale]
        if len(keep) != len(entries):
            removed += len(entries) - len(keep)
            data["entries"] = keep
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return removed


def write_adjudications(b: "Bundle", source_id: str, entries: list[dict]) -> list[str]:
    """Merge entries into documents/<sid>/adjudications.json, in the engine's schema.

    Returns the reasons any entry was skipped. Deduplicates on
    (page, check, fingerprint), which is the key the engine uses, and tolerates a
    legacy bare-list file written before this was schema-correct.
    """
    path = b.adjudications_path(source_id)
    existing: list[dict] = []
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            existing = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
        elif isinstance(data, list):                  # legacy shape; migrate it
            existing = [e for e in data if isinstance(e, dict)]

    skipped: list[str] = []
    merged = {(e.get("page"), e.get("check"), e.get("fingerprint")): e for e in existing}
    for e in entries:
        why = valid_adjudication(e)
        if why:
            skipped.append(f"p{e.get('page')} {e.get('check')}: {why}")
            continue
        merged[(e["page"], e["check"], e["fingerprint"])] = e

    # Drop any pre-existing entry that would make the engine reject the whole file.
    clean, dropped = [], 0
    for e in merged.values():
        if valid_adjudication(e):
            dropped += 1
            continue
        clean.append({"page": e["page"], "check": e["check"],
                      "fingerprint": e["fingerprint"], "reason": e["reason"].strip()})
    if dropped:
        skipped.append(f"dropped {dropped} pre-existing invalid entr(ies)")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"schema_version": ADJ_SCHEMA_VERSION, "entries": clean},
        indent=2, ensure_ascii=False))
    return skipped
