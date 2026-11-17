"""End-to-end conversion: prepare -> route -> extract -> review -> build -> verify.

The coordinator is plain Python. It owns bundle.json and every build/verify call;
agents only ever edit page plans and adjudications. `verify --strict` exit code
is the termination signal, so the loop has an external stopping criterion rather
than a model's opinion of its own work.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .bundle import (Bundle, DONE, check_symbol_repairs, collect_diagnostics,
                     hoist_floats, repair_builder_constraints, repair_cross_page_joins, repair_degree_signs,
                     repair_dropped_symbols,
                     prune_stale_adjudications, unreviewed_pages, write_adjudications)
from .accounting import account_work
from .crops import repair_crops
from .metrics import paper_metrics
from .ocr import ocr_note, ocr_substitute
from .delivery import inspect_package, summarize
from .index import archive_path, record_from_report, update_index
from .sources import copy_source, original_pdf
from .notes import write_notes
from .limitations import draft_limitations, merge_notes, render_markdown
from .synthesize import synthesize
from .tablefill import fill_image_tables
from .tablefix import repair_prose_tables
from .config import Config
from .cost import Ledger
from .prompts import ADJUDICATE_INSTRUCTIONS, BUNDLE_INSTRUCTIONS
from .review import queue_notes_by_page, review_page


# --- stage 1: route the sources ---------------------------------------------

class SourceRouting(BaseModel):
    source_id: str
    role: Literal["main", "supplement", "tables", "attachment"]
    title: str
    asset_name: str = Field(description="lowercase, hyphenated, no extension")


class BundlePlan(BaseModel):
    name: str
    title: str
    sources: list[SourceRouting]
    notes: list[str] = Field(
        description="Published with the package. Each note records something a reader "
                    "would otherwise be misled by: a cited supplement that was not "
                    "supplied, an appended corrigendum, a known extraction limitation.")


async def route_sources(b: Bundle, cfg: Config, ledger, client) -> BundlePlan | None:
    inv, cur = b.inventory(), b.bundle_json()
    if not inv:
        return None
    t0 = time.time()
    resp = await client.responses.parse(
        model=cfg.review_model,
        instructions=BUNDLE_INSTRUCTIONS,
        input=[{"role": "user", "content":
                "INVENTORY:\n" + json.dumps(inv, indent=1)[:40000] +
                "\n\nCURRENT bundle.json:\n" + json.dumps(cur, indent=1)[:20000]}],
        text_format=BundlePlan,
    )
    ledger.record(stage="bundle", unit="-", model=cfg.review_model, usage=resp.usage,
                  seconds=time.time() - t0, note="route_sources")
    plan = resp.output_parsed

    # Merge into bundle.json without dropping keys the CLI wrote.
    data = dict(cur)
    if plan.title:
        data["title"] = plan.title
    data["notes"] = list(plan.notes)
    by_id = {s.source_id: s for s in plan.sources}
    for src in data.get("sources", []):
        r = by_id.get(src.get("id")) or by_id.get(src.get("source_id"))
        if r:
            src["role"] = r.role
            src["title"] = r.title
            # A slug derived from a long filename can run to 160+ characters and
            # becomes the asset directory name; keep it readable.
            src["asset_name"] = (r.asset_name or "")[:55].rstrip("-") or src.get("asset_name")
    b.write_bundle_json(data)
    return plan


# --- stage 4: adjudicate parser-only diagnostics -----------------------------

class Diagnostic(BaseModel):
    source_id: str
    page: int
    check: str
    verdict: Literal["parser_only", "repair_needed", "unclear"]
    reason: str = Field(description="What you actually saw, naming the specific tokens.")


class Adjudications(BaseModel):
    decisions: list[Diagnostic]


async def adjudicate(b: Bundle, cfg: Config, ledger, client,
                     diagnostics: list[dict]) -> tuple[Adjudications, list[str]]:
    """Classify each diagnostic and write the parser-only ones.

    Returns the decisions and the reasons any entry could not be written.
    """
    if not diagnostics:
        return Adjudications(decisions=[]), []
    payload = [{k: v for k, v in d.items() if k != "adjudication_entry"}
               for d in diagnostics]
    t0 = time.time()
    resp = await client.responses.parse(
        model=cfg.review_model,
        instructions=ADJUDICATE_INSTRUCTIONS,
        input=[{"role": "user", "content":
                "DIAGNOSTICS:\n" + json.dumps(payload, indent=1, ensure_ascii=False)[:120000]}],
        text_format=Adjudications,
    )
    ledger.record(stage="adjudicate", unit="-", model=cfg.review_model, usage=resp.usage,
                  seconds=time.time() - t0, note=f"{len(diagnostics)} diagnostics")
    out = resp.output_parsed

    by_src: dict[str, list[dict]] = {}
    for d in out.decisions:
        if d.verdict != "parser_only":
            continue
        for q in diagnostics:
            if (q["source_id"] == d.source_id and q["page"] == d.page
                    and q["check"] == d.check):
                entry = dict(q["adjudication_entry"])
                entry["reason"] = d.reason
                by_src.setdefault(d.source_id, []).append(entry)
                break

    skipped: list[str] = []
    for sid, entries in by_src.items():
        skipped += write_adjudications(b, sid, entries)
    return out, skipped


def roll_up_source_review(b: Bundle, page_records: list[dict], cfg: Config,
                          log, extra_notes: list[str] | None = None) -> list[str]:
    """Set each source's `reviewed` flag in bundle.json once all its pages are done.

    There are two review flags, and both gate the build:
      * per page, in documents/<sid>/pages/page-NNNN.json  (the agent sets these)
      * per source, in bundle.json sources[].reviewed       (nobody was setting this)

    `paper_bundle.load_bundle` validates that a source marked reviewed carries
    non-empty `review_notes`, so the note is required, not decorative. The
    coordinator owns bundle.json, and this note is a factual roll-up of what the
    page pass actually did -- counts of applied operations, taken from the editor's
    own op log rather than from anything a model asserted.

    Returns the ids of sources that could NOT be marked reviewed.
    """
    data = b.bundle_json()
    pending: list[str] = []
    by_source: dict[str, list[dict]] = {}
    for r in page_records:
        by_source.setdefault(r["source"], []).append(r)

    for src in data.get("sources", []):
        sid = src.get("id")
        pages = b.pages(sid)
        if not pages:
            # A workbook, standalone figure or opaque attachment has no page plans.
            # This harness reviews PDF pages only, so it must not claim to have
            # reviewed one. Leave it and let the build name it.
            if not src.get("reviewed"):
                pending.append(sid)
                log(f"  source {sid} has no page plans (not a paged PDF); this harness "
                    "does not review it, so the build will refuse until it is handled")
            continue

        unreviewed = [p for p in pages
                      if not json.loads(p.read_text()).get("reviewed")]
        if unreviewed:
            pending.append(sid)
            continue

        recs = by_source.get(sid, [])
        ops: dict[str, int] = {}
        for r in recs:
            for op in r.get("ops", []):
                ops[op.split("(")[0]] = ops.get(op.split("(")[0], 0) + 1
        limits = [lim for r in recs for lim in (r.get("limitations") or [])]
        op_text = ", ".join(f"{k}: {v}" for k, v in sorted(ops.items())) or "none"

        note = (
            f"All {len(pages)} pages of this source were inspected individually against "
            f"{cfg.preview_dpi}-dpi page renders: reading order across columns, section "
            f"hierarchy, prose continuing across page breaks, figure and table crop "
            f"boundaries, and captions. Applied operations by type -- {op_text}. "
            f"Figure and table crops were rendered at {cfg.asset_dpi} dpi with a "
            f"{cfg.max_image_side} px cap on the longest side. "
            f"Reviewed by an automated per-page review agent ({cfg.review_model}); "
            f"per-page notes and the exact operations applied to each page are recorded in "
            f"documents/{sid}/pages/. This records the review that was performed, not "
            f"independently certified accuracy."
        )
        if limits:
            note += " Limitations recorded during review: " + "; ".join(limits[:12]) + "."
        for extra in (extra_notes or []):
            note += " " + extra
        src["reviewed"] = True
        src["review_notes"] = note

    b.write_bundle_json(data)
    done = [s.get("id") for s in data.get("sources", []) if s.get("reviewed")]
    if done:
        log(f"marked {len(done)} source(s) reviewed in bundle.json")
    return pending


# --- output paths ------------------------------------------------------------
# `reading_package.build` refuses an output directory that already exists, that
# sits inside the review directory, or that contains it:
#     if output.exists() or output.is_relative_to(work) or work.is_relative_to(output)
# So every build needs a brand-new directory outside the review directory.

def check_paths(work: Path, output: Path) -> str:
    """Validate before any token is spent. Returns '' when the paths are usable."""
    work, output = work.resolve(), output.resolve()
    if output == work:
        return "--out and --work cannot be the same directory."
    if output.is_relative_to(work):
        return (f"--out ({output}) is inside --work ({work}). The package must be "
                "built outside the review directory; choose a path that is not under it.")
    if work.is_relative_to(output):
        return (f"--work ({work}) is inside --out ({output}). The review directory must "
                "not sit inside the package; choose a --work outside it.")
    return ""


def fresh_dir(base: Path) -> Path:
    """A path that does not exist, derived from `base`, with its parent created."""
    base.parent.mkdir(parents=True, exist_ok=True)
    if not base.exists():
        return base
    for n in range(2, 1000):
        cand = base.parent / f"{base.name}-{n}"
        if not cand.exists():
            return cand
    return base.parent / f"{base.name}-{int(time.time())}"


def clear_for_build(output: Path, log) -> None:
    """Archive an existing package so a rebuild can use the same path.

    The previous build goes to <parent>/_archive/<name>/<timestamp>/, so the folder
    named for the paper is always the latest one (see index.py)."""
    if not output.exists():
        return
    backup = archive_path(output)
    backup.parent.mkdir(parents=True, exist_ok=True)
    output.rename(backup)
    log(f"archived the previous build to {backup}")


def pending_sources_only(b: Bundle, cfg: Config) -> bool:
    """True when pages are still unreviewed, so document-level synthesis would be
    working from an incomplete document."""
    return bool(cfg.require_reviewed and unreviewed_pages(b))


# --- the conversion ----------------------------------------------------------

async def convert(inputs: list[Path], name: str, work: Path, output: Path,
                  cfg: Config, *, title: str | None = None, main: Path | None = None,
                  attachments: list[Path] | None = None,
                  pages: list[int] | None = None, concurrency: int = 3,
                  force: bool = False, resume: bool = False,
                  reuse_from: Path | None = None,
                  ocr_pages: list[int] | None = None) -> dict:
    work, output = Path(work).resolve(), Path(output).resolve()
    path_problem = check_paths(work, output)
    if path_problem:
        print(f"[paperskill] {path_problem}")
        return {"name": name, "error": path_problem}

    run_dir = work / "agent-run"
    ledger = Ledger(run_dir, cfg.pricing)
    client = AsyncOpenAI()
    b = Bundle(work, cfg, ledger)
    report: dict = {"name": name, "work": str(work), "output": str(output),
                    "config": cfg.as_dict(), "stages": {}}

    def log(msg: str) -> None:
        print(f"[paperskill] {msg}", flush=True)

    def save_report() -> None:
        """Persist after every stage. The review pass is the expensive part of a
        run, so it must survive a failure in anything that comes after it."""
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            report["cost"] = ledger.summary()
            report["metrics"] = paper_metrics(report, b.plan_dirs)
            (run_dir / "report.json").write_text(
                json.dumps(Ledger._round(report), indent=2, ensure_ascii=False))
        except (OSError, TypeError, ValueError) as exc:
            log(f"could not write report.json: {exc}")

    # 1. prepare
    if not resume and work.exists() and any(work.iterdir()):
        report["error"] = (f"{work} already exists and is not empty. `prepare` will not "
                           "reuse a review directory: pass --resume to keep the existing "
                           "extraction, or --work <new path> to start clean.")
        print(f"[paperskill] {report['error']}")
        return report

    ocr_done: list[int] = []
    if ocr_pages and (not resume or not (work / "inventory.json").exists()):
        target = main or inputs[0]
        copy = work.parent / f"{work.name}-ocr-input" / target.name
        res = ocr_substitute(target, copy, ocr_pages)
        ocr_done = res["done"]
        report["stages"]["ocr"] = {"source": str(target), "copy": str(copy), **res}
        log(f"ocr: pages {ocr_done} replaced by OCR text"
            + (f"; failed {res['failed']}" if res["failed"] else ""))
        inputs = [copy if p == target else p for p in inputs]
        main = copy if main == target else main

    if not resume or not (work / "inventory.json").exists():
        log(f"prepare: {len(inputs)} input(s) -> {work}")
        r = b.prepare(inputs, name, title, main, attachments)
        report["stages"]["prepare"] = {"ok": r.ok, "seconds": round(r.seconds, 1)}
        if not r.ok:
            report["error"] = f"prepare failed: {r.stderr.strip()[:400]}"
            ledger.flush()
            return report

    # 2. route sources (one structured call)
    log("routing sources into bundle.json")
    plan = await route_sources(b, cfg, ledger, client)
    if not title and plan:
        main = next((s for s in plan.sources if s.role == "main"), None)
        if main and main.title and len(main.title) > len(name):
            data = b.bundle_json()
            data["title"] = main.title
            b.write_bundle_json(data)
            log(f"title set from the main source: {main.title[:70]}")
    report["stages"]["route"] = {"notes": plan.notes if plan else [],
                                 "sources": [s.model_dump() for s in plan.sources] if plan else []}

    # 3. extract
    if not resume or not (work / "documents").exists():
        log(f"extract at {cfg.preview_dpi} dpi previews")
        r = b.extract()
        report["stages"]["extract"] = {"ok": r.ok, "seconds": round(r.seconds, 1)}
        if not r.ok:
            report["error"] = f"extract failed: {r.stderr.strip()[:400]}"
            ledger.flush()
            return report

    # The layout model sometimes boxes ordinary paragraphs as word-per-cell tables
    # (paper 15: 74 of 77). Rebuild them as prose before any model reads the page;
    # reviewed pages are never touched, so this is safe on --resume.
    prose = repair_prose_tables(b.plan_dirs)
    if prose:
        report["stages"]["prose_table_repairs"] = prose
        log(f"layout: {len(prose)} region(s) labelled as tables were rebuilt as prose")

    if reuse_from:
        log(f"reusing review decisions from {reuse_from}")
        r = b.reuse_review(reuse_from)
        report["stages"]["reuse_review"] = {"ok": r.ok, "data": r.data}

    b.review_aid()
    notes_idx = queue_notes_by_page(b.review_queue())

    # 4. review every page of every document
    targets: list[tuple[str, int]] = []
    for d in b.plan_dirs:
        for p in b.pages(d.name):
            pg = int(p.stem.split("-")[-1])
            if pages is None or pg in pages:
                targets.append((d.name, pg))
    log(f"reviewing {len(targets)} page(s) with {cfg.review_model}, concurrency {concurrency}")

    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(sid: str, pg: int) -> dict:
        nonlocal done
        async with sem:
            rec = await review_page(b, cfg, ledger, client, sid, pg,
                                    queue_notes=notes_idx.get((sid, pg)), force=force)
            done += 1
            cost = ledger.summary()["total"]["usd"]
            log(f"  [{done}/{len(targets)}] {sid} p{pg}: {rec['status']}"
                f"  ({len(rec.get('ops', []))} edits, ${cost:.3f} so far)")
            return rec

    page_records = await asyncio.gather(*(one(s, p) for s, p in targets))
    report["stages"]["review"] = {"pages": page_records}
    ledger.flush()
    save_report()

    # 5. build / verify / adjudicate loop
    # The one pass that sees the whole document. Must run after review (it needs
    # the final headings) and before the build (it writes bundle.json).
    if not pending_sources_only(b, cfg):
        log("synthesizing navigation and notes")
        try:
            report["stages"]["synthesis"] = await synthesize(b, cfg, ledger, client, log)
        except Exception as exc:                      # never block a build on it
            log(f"synthesis failed ({type(exc).__name__}: {exc}); continuing without it")
            report["stages"]["synthesis"] = {"error": f"{type(exc).__name__}: {exc}"}
        save_report()

    # Both review flags must be set before a build: the per-page ones the agent
    # writes, and the per-source one in bundle.json rolled up from them.
    pending_sources = roll_up_source_review(b, page_records, cfg, log)
    report["stages"]["source_review"] = {"pending": pending_sources}
    save_report()

    # sibling of the review directory, never inside it, and a new leaf each attempt
    # `load_adjudications` runs at the START of every build and rejects the whole
    # file if its shape or any entry is wrong -- so a malformed adjudications.json
    # fails attempt 1, before the adjudication step that would have rewritten it.
    # Normalize any existing file first.
    for d in b.plan_dirs:
        p = b.adjudications_path(d.name)
        if p.exists():
            fixed = write_adjudications(b, d.name, [])
            n = len(json.loads(p.read_text()).get("entries") or [])
            log(f"adjudications: {n} entr(ies) for {d.name[:40]}"
                + (f", {len(fixed)} skipped" if fixed else ""))
            for why in fixed:
                log(f"  {why}")

    stage_root = work.parent / f"{work.name}-staging"
    last_recoverable_error = ""
    rereviewed: set[tuple[str, int]] = set()
    symbol_fixed: set[tuple[str, int]] = set()
    adjudicated_rounds = 0
    for attempt in range(1, cfg.max_build_verify_rounds + 1):
        # `build --require-reviewed` refuses outright if any page is unreviewed, so
        # finish those first rather than spending a build to be told.
        if cfg.require_reviewed:
            pending_pages = unreviewed_pages(b)
            if pending_pages:
                log(f"{len(pending_pages)} page(s) still unreviewed; retrying them before build")
                for sid, pg in pending_pages:
                    rec = await review_page(b, cfg, ledger, client, sid, pg,
                                            queue_notes=notes_idx.get((sid, pg)), force=True)
                    log(f"  retry {sid} p{pg}: {rec['status']}")
                    page_records = [r for r in page_records
                                    if not (r["source"] == sid and r["page"] == pg)] + [rec]
                report["stages"]["review"] = {"pages": page_records}
                roll_up_source_review(b, page_records, cfg, log)
                save_report()
                still = unreviewed_pages(b)
                if still:
                    log(f"  {len(still)} page(s) could not be completed: "
                        f"{[f'p{p}' for _, p in still]}")
                    report["error"] = (
                        f"{len(still)} page(s) could not be marked reviewed: "
                        f"{[f'p{p}' for _, p in still]}. Their edits are saved. Re-run with "
                        "--resume to retry only those, raise --max-turns-per-page, or pass "
                        "--draft to build anyway.")
                    break

        # A figure or table dropped between two halves of a sentence passes every
        # verification check (line coverage ignores order) yet reads wrongly. The
        # fix is mechanical and idempotent, so it runs before every build.
        fixes_bc = (repair_builder_constraints(b) + repair_cross_page_joins(b)
                    + repair_degree_signs(b))
        if fixes_bc:
            report["stages"]["builder_constraint_repairs"] = fixes_bc
            for f in fixes_bc:
                log(f"  repaired p{f['page']}: {f['detail']}")
        hoists = hoist_floats(b)
        if hoists:
            report["stages"]["float_hoists"] = hoists
            n_moved = sum(1 for h in hoists if h["applied"])
            log(f"floats: {n_moved} moved off mid-sentence positions, "
                f"{len(hoists) - n_moved} left (see report)")
            for h in hoists:
                log(f"  {'moved ' if h['applied'] else 'LEFT  '} p{h['page']}: {h['detail']}")
        else:
            n_moved = 0
        crops_fixed = repair_crops(b.plan_dirs)
        n_grown = sum(1 for c in crops_fixed if c["applied"])
        if crops_fixed:
            report["stages"]["crop_repairs"] = crops_fixed
            log(f"crops: {n_grown} enlarged to include text they cut off, "
                f"{len(crops_fixed) - n_grown} left (see report)")
            for c in crops_fixed:
                log(f"  {'grew  ' if c['applied'] else 'LEFT  '} p{c['page']}: {c['detail']}")
        n_tables = 0
        if cfg.table_fallback:
            filled = await fill_image_tables(b.plan_dirs, cfg, ledger, client, concurrency)
            if filled:
                report["stages"].setdefault("table_fallback", []).extend(filled)
                n_tables = sum(1 for f in filled if f["applied"])
                log(f"tables: {n_tables} of {len(filled)} image table(s) transcribed by the "
                    "fallback, the rest left as images (see report)")
                for f in filled:
                    log(f"  {'filled' if f['applied'] else 'LEFT  '} p{f['page']} {f['item']}: "
                        + (f"{f['verdict']}, {f['rows']} rows" if f["applied"]
                           else f.get("why", "")))
                notes = [f["note"] for f in filled if f["applied"]
                         and f["verdict"] != "accepted"]
                if notes:
                    data = b.bundle_json()
                    have = data.get("notes") or []
                    data["notes"] = have + [n for n in notes if n not in have]
                    b.write_bundle_json(data)
                save_report()
        extra = []
        if n_tables:
            extra.append(f"{n_tables} table(s) the page review left as images were transcribed "
                         "by a deterministic fallback and cross-checked against the PDF text.")
        if n_moved:
            extra.append(f"{n_moved} figure/table block(s) that interrupted a sentence were "
                         "moved to the nearest paragraph break deterministically; no content "
                         "was changed.")
        if n_grown:
            extra.append(f"{n_grown} figure/table crop(s) that cut off their own text were "
                         "enlarged deterministically; no content was changed.")
        if extra:
            roll_up_source_review(b, page_records, cfg, log, extra_notes=extra)

        # A mangled comparison symbol that was DELETED instead of restored changes
        # what the paper reports (an open bound becomes a closed one) and no later
        # check can see it. The repair is mechanical -- the raw extraction records
        # which symbol belongs where -- and two agent passes failed at it, so it is
        # applied deterministically and the agent is only a fallback.
        dropped = [d for d in check_symbol_repairs(b) if d["verdict"] == "symbol_dropped"]
        if dropped:
            log(f"{len(dropped)} dropped comparison symbol(s); repairing deterministically")
            fixes = repair_dropped_symbols(b)
            for f in fixes:
                log(f"  {'fixed ' if f['applied'] else 'FAILED'} p{f['page']}: {f['detail']}")
            report["stages"]["symbol_repairs"] = fixes
            still = [d for d in check_symbol_repairs(b) if d["verdict"] == "symbol_dropped"]

            # Anything the mechanical fix could not place (ambiguous or not found)
            # goes to one agent pass per page, naming the exact lines.
            todo = [d for d in still if (d["source_id"], d["page"]) not in symbol_fixed]
            if todo:
                log(f"  {len(todo)} not placed mechanically; one agent pass per page")
                ssem = asyncio.Semaphore(max(1, concurrency))

                async def fix_symbols(sid: str, pg: int, lines: list[str]) -> None:
                    async with ssem:
                        symbol_fixed.add((sid, pg))
                        note = ("A comparison symbol was DELETED rather than restored on "
                                "this page. In this publisher's extraction a capital 'N' "
                                "before a number is '>' and a lower-case 'b' before a "
                                "number is '<'. Put the symbol back; do not remove it. "
                                "Affected source lines: " + " | ".join(lines))
                        rec = await review_page(b, cfg, ledger, client, sid, pg,
                                                queue_notes=[note], force=True)
                        log(f"    agent repair {sid} p{pg}: {rec['status']}")

                by_page: dict[tuple[str, int], list[str]] = {}
                for d in todo:
                    by_page.setdefault((d["source_id"], d["page"]), []).append(d["raw"])
                await asyncio.gather(*(fix_symbols(sid, pg, lines)
                                       for (sid, pg), lines in by_page.items()))
                still = [d for d in check_symbol_repairs(b)
                         if d["verdict"] == "symbol_dropped"]

            report["stages"]["symbol_checks"] = check_symbol_repairs(b)
            if any(f["applied"] for f in fixes):
                roll_up_source_review(
                    b, page_records, cfg, log,
                    extra_notes=[f"{sum(1 for f in fixes if f['applied'])} comparison "
                                 "symbol(s) deleted during page review were restored "
                                 "deterministically from the raw extraction."])
            if still:
                log(f"  {len(still)} symbol(s) still dropped")
                report["error"] = (
                    f"{len(still)} comparison symbol(s) were deleted rather than restored "
                    "and could not be repaired. This changes reported bounds, so the "
                    "package is not trustworthy as built. The affected lines are in "
                    "report.json under stages.symbol_checks.")
                save_report()
                break
        else:
            report["stages"]["symbol_checks"] = check_symbol_repairs(b)

        ocr_pages_done = (report["stages"].get("ocr") or {}).get("done") or ocr_done
        if ocr_pages_done:
            data = b.bundle_json()
            note = ocr_note(ocr_pages_done)
            if note not in (data.get("notes") or []):
                data["notes"] = list(data.get("notes") or []) + [note]
                b.write_bundle_json(data)
        accounted = account_work(b.plan_dirs)
        report["stages"]["word_accounting"] = accounted
        if accounted:
            log(f"word accounting: {len(accounted)} page(s) have words the package does not "
                "carry (see report.json stages.word_accounting)")

        staging = fresh_dir(stage_root / f"attempt-{attempt}" / name)
        log(f"build (attempt {attempt}) at {cfg.asset_dpi} dpi -> {staging}")
        rb = b.build(staging)
        if not rb.ok:
            log(f"  BUILD FAILED: {rb.error}")
            log(f"  full output: {rb.log}")
        b.review_aid()
        rv = b.verify(strict=True)
        status = rv.status or "unknown"
        log(f"verify: status={status} exit={rv.code}")
        for w in (rb.warnings, rv.warnings):
            if "fontTools is required" in w:
                log("  note: fontTools is not installed. It parses embedded font "
                    "encodings, which is where mangled comparison symbols come from. "
                    "`pip install fonttools` and re-extract may avoid them at source.")
                break
        # exit 2 is a pending *state* and carries JSON; exit 1 means the CLI
        # itself errored and printed only stderr. Surface that immediately --
        # it is never recoverable by looping.
        if rv.code == 1:
            log(f"  VERIFY ERRORED: {rv.error}")
            log(f"  full output: {rv.log}")
        report["stages"].setdefault("build_verify", []).append(
            {"attempt": attempt, "build_ok": rb.ok, "status": status, "exit": rv.code,
             "build_error": rb.error, "verify_error": rv.error,
             "build_log": str(rb.log) if rb.log else None,
             "verify_log": str(rv.log) if rv.log else None})

        # An unreviewed-pages refusal is recoverable (the retry above handles it);
        # anything else from the builder is not.
        recoverable = "have not been reviewed" in (rb.error or "")
        if (not rb.ok and not recoverable) or (rv.code == 1 and not recoverable):
            report["error"] = (f"build/verify failed outside the review loop: "
                              f"{rb.error or rv.error}")
            break
        if not rb.ok and recoverable:
            # Retrying only helps if something changed. Spinning the full round
            # budget on an identical refusal wastes time and says nothing.
            if rb.error == last_recoverable_error:
                pend = roll_up_source_review(b, page_records, cfg, log)
                report["error"] = (
                    "the build keeps refusing for the same reason and nothing changed "
                    f"between attempts: {rb.error}"
                    + (f" Sources not marked reviewed: {pend}." if pend else
                       " Every source is marked reviewed, so this is a builder-side"
                       " condition -- see the build log.")
                    + " Pass --draft to build anyway.")
                break
            last_recoverable_error = rb.error
            continue
        if status == "unresolved_discrepancies":
            n_stale = prune_stale_adjudications(b)
            if n_stale:
                log(f"  dropped {n_stale} stale adjudication(s) left by a page re-review; "
                    "re-verifying")
                rv = b.verify(strict=True)
                status = rv.status or "unknown"
                log(f"verify: status={status} exit={rv.code}")
                report["stages"]["build_verify"][-1].update(
                    {"status": status, "exit": rv.code, "pruned_stale": n_stale})
        if status in DONE and rv.code == 0:
            break
        if status == "unresolved_discrepancies":
            diags = collect_diagnostics(b)
            log(f"adjudicating {len(diags)} parser diagnostic(s)")
            adj, adj_skipped = await adjudicate(b, cfg, ledger, client, diags)
            counts: dict[str, int] = {}
            for d in adj.decisions:
                counts[d.verdict] = counts.get(d.verdict, 0) + 1
            log(f"  verdicts: {counts or 'none'}")
            report["stages"].setdefault("adjudications", []).append(
                {"attempt": attempt, "diagnostics": len(diags),
                 "skipped": adj_skipped,
                 "decisions": [d.model_dump() for d in adj.decisions]})
            for why in adj_skipped:
                log(f"  skipped adjudication -- {why}")

            # A page is re-reviewed at most once per run. These diagnostics are
            # tokenization differences; if re-reading the page did not resolve one,
            # re-reading it again never will, and the loop would spin.
            repairs = [d for d in adj.decisions
                       if d.verdict != "parser_only"
                       and (d.source_id, d.page) not in rereviewed]
            stuck = [d for d in adj.decisions
                     if d.verdict != "parser_only"
                     and (d.source_id, d.page) in rereviewed]
            if not adj.decisions:
                break
            if not repairs and not stuck:
                # Every diagnostic was parser-only and its adjudication has been
                # written. The whole point is to rebuild and re-verify so they are
                # applied -- breaking here would leave them unused.
                log("  all parser-only; rebuilding so the adjudications apply")
                if adjudicated_rounds >= 2:
                    report["error"] = ("adjudications were written twice and "
                                       "verification still reports them unresolved; "
                                       "check documents/<source>/adjudications.json "
                                       "against the review queue for stale fingerprints.")
                    break
                adjudicated_rounds += 1
                continue
            if repairs:
                log(f"  re-reviewing {len(repairs)} page(s) once "
                    f"(concurrency {max(1, concurrency)})")
                rsem = asyncio.Semaphore(max(1, concurrency))

                async def repair(d) -> None:
                    async with rsem:
                        rereviewed.add((d.source_id, d.page))
                        rec = await review_page(
                            b, cfg, ledger, client, d.source_id, d.page,
                            queue_notes=[f"verifier ({d.check}): {d.reason}"], force=True)
                        log(f"    {d.source_id} p{d.page}: {rec['status']}")

                await asyncio.gather(*(repair(d) for d in repairs))
                roll_up_source_review(b, page_records, cfg, log)
            if stuck and not repairs:
                names = sorted({f"p{d.page} {d.check}" for d in stuck})
                log(f"  {len(stuck)} diagnostic(s) survived a re-review: {names}")
                report["error"] = (
                    f"{len(stuck)} verification diagnostic(s) could not be resolved by "
                    f"re-reviewing the page: {names}. They are listed with their evidence "
                    "in report.json under stages.adjudications. Inspect the cited pages "
                    "and either copy the matching adjudication_entry into "
                    "documents/<source>/adjudications.json with what you saw, or accept "
                    "the package as a draft.")
                break
        elif status == "unreviewed":
            unreviewed = [r for r in page_records if r["status"] != "reviewed"]
            if not cfg.require_reviewed and not unreviewed:
                # draft mode over a --pages subset: every targeted page is done and
                # the rest of the document was never meant to be reviewed. The build
                # succeeded, which is what this run was testing.
                log("  draft build complete; remaining pages were outside --pages")
                break
            log(f"  {len(unreviewed)} page(s) not reviewed; retrying those")
            if not unreviewed:
                break
            for r in unreviewed:
                await review_page(b, cfg, ledger, client, r["source"], r["page"], force=True)
        else:
            break
        ledger.flush()
        save_report()

    # 6. final build into the delivery directory
    rounds = report["stages"].get("build_verify") or []
    if not rounds:
        # A pre-build gate stopped the run, so there is no build to report on.
        report["final"] = {"build_ok": False, "status": "blocked",
                           "note": report.get("error", "stopped before the first build"),
                           "output": None}
        ledger.flush()
        save_report()
        print(ledger.report())
        return report
    final_status = rounds[-1]["status"]
    if final_status == "unreviewed" and not cfg.require_reviewed:
        last = rounds[-1]
        report["final"] = {"build_ok": last["build_ok"], "status": "draft",
                           "note": "draft build from a page subset; not a deliverable",
                           "output": str(staging)}
    elif final_status in DONE:
        log(f"final build -> {output}")
        clear_for_build(output, log)
        stale = output.parent / f"{output.name}.LIMITATIONS.md"
        if stale.exists():     # an earlier draft of this paper; it no longer applies
            keep = archive_path(output).with_suffix(".LIMITATIONS.md")
            keep.parent.mkdir(parents=True, exist_ok=True)
            stale.rename(keep)
        rb = b.build(output)
        if not rb.ok:
            log(f"  FINAL BUILD FAILED: {rb.error}  (full output: {rb.log})")
        rv = b.verify(strict=True)
        report["final"] = {"build_ok": rb.ok, "status": rv.status, "exit": rv.code,
                           "output": str(output), "build_error": rb.error,
                           "verify_error": rv.error}
        if rb.ok:
            findings = inspect_package(output, work)
            fails, warns, text = summarize(findings)
            log(f"delivery inspection: {fails} fail, {warns} warn")
            if text:
                for line in text.splitlines():
                    log(line)
            report["stages"]["delivery"] = findings
            report["final"]["delivery_fails"] = fails
            report["final"]["delivery_warns"] = warns
    elif rounds[-1].get("build_ok") and final_status != "unreviewed":
        # Strict verification did not pass, but a build exists. Withholding it hides
        # the part that is fine, so deliver it labelled: the limitations go into
        # bundle.json `notes` (published inside the package, which cannot take an
        # extra file because the manifest must match) and a sidecar LIMITATIONS.md.
        reason = report.pop("error", "") or "strict verification did not pass"
        lims = draft_limitations(
            collect_diagnostics(b),
            extra=[reason] + [f"crop on page {c['page']}: {c['detail']}"
                              for c in report['stages'].get('crop_repairs', [])
                              if not c['applied']],
                                 accounting=report['stages'].get('word_accounting'))
        data = b.bundle_json()
        data["notes"] = merge_notes(list(data.get("notes") or []), lims)
        b.write_bundle_json(data)
        log(f"delivering as a DRAFT with {len(lims)} listed limitation(s) -> {output}")
        clear_for_build(output, log)
        rb = b.build(output)
        rv = b.verify(strict=True)
        sidecar = output.parent / f"{output.name}.LIMITATIONS.md"
        sidecar.write_text(render_markdown(name, lims, reason))
        report["final"] = {"build_ok": rb.ok, "status": "delivered_draft" if rb.ok else "blocked",
                           "exit": rv.code, "verify_status": rv.status,
                           "output": str(output), "limitations": lims,
                           "limitations_file": str(sidecar), "draft_reason": reason,
                           "note": f"DRAFT: {len(lims)} limitation(s) listed in {sidecar.name}",
                           "build_error": rb.error, "verify_error": rv.error}
        if rb.ok:
            findings = inspect_package(output, work)
            fails, warns, _ = summarize(findings)
            report["stages"]["delivery"] = findings
            report["final"]["delivery_fails"] = fails
            report["final"]["delivery_warns"] = warns
    else:
        report["final"] = {"build_ok": False, "status": final_status,
                           "note": "left in staging; strict verification did not pass",
                           "output": str(staging),
                           "build_error": rounds[-1].get("build_error", ""),
                           "verify_error": rounds[-1].get("verify_error", "")}

    if report.get("final", {}).get("status") in {"reviewed", "reviewed_with_limitations",
                                                 "delivered_draft"}:
        try:
            nf = write_notes(output, name, list(b.bundle_json().get("notes") or []),
                             b.plan_dirs)
            report["final"]["notes_file"] = str(nf)
            log(f"notes written to {nf}")
        except OSError as exc:
            log(f"could not write the notes file: {exc}")
    ledger.flush()
    save_report()
    if report.get("final", {}).get("status") in {"reviewed", "reviewed_with_limitations",
                                                 "delivered_draft"}:
        try:
            orig = original_pdf(work, (report["stages"].get("ocr") or {}).get("source"))
            kept = copy_source(output, orig) if orig else None
            if kept:
                log(f"original PDF copied to {kept}")
            idx = update_index(output.parent, record_from_report(
                report, source=str(kept or "")))
            log(f"index updated: {idx}")
        except OSError as exc:
            log(f"could not update the index: {exc}")
    print(ledger.report())
    return report
