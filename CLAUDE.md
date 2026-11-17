# paperskill-agent — working notes

Converts a research PDF into a reviewed, verified skill package through the OpenAI
Agents SDK, and reports the exact cost of each conversion. Wraps the `paper2skill`
engine unmodified (`vendor/`, three standalone scripts). See `README.md` for usage.

This file is the context a new session needs. Most of it was learned by running the
thing on a real paper and finding out; the reasoning matters more than the code.

## Commands

```bash
pytest tests/ -q                     # 187 tests, no API key, no network
python -m pyflakes paperskill run.py # must be silent
python run.py pricing                # active price table

python run.py convert PAPER.pdf --name x --out dist/x --pages 1,2 --draft   # cheap smoke
python run.py convert PAPER.pdf --name x --out dist/x                       # full (~$1.35, ~10 min)
python run.py convert PAPER.pdf --name x --out dist/x --resume              # reuse reviewed pages
python run.py cost runs/x-review                                            # re-print cost report
```

`--resume` costs almost nothing: reviewed pages return `cached` with no API call.
Use it for every iteration after the first.

## Architecture

| stage | model? | agent? | what it does |
|---|---|---|---|
| prepare | no | no | snapshot sources, write `inventory.json` |
| route | yes | no | one structured call: roles, titles, asset names, published `notes` |
| extract | no | no | per-page plans, evidence, previews |
| **review** | yes | **yes** | one fresh agent session *per page*, with tools |
| float gate | no | no | `hoist_floats`: move a figure/table off a mid-sentence position |
| crop gate | no | no | `repair_crops`: grow a figure/table crop that cuts off its own text |
| word accounting | no | no | `account_work`: every PDF word is in a crop, retained text, or flagged |
| symbol gate | no | no | deterministic repair of deleted comparison symbols |
| build | no | no | `build --require-reviewed` |
| verify | no | no | `verify --strict` — **exit 0 or 2** |
| adjudicate | yes | no | classify parser-only diagnostics, then rebuild |

The coordinator is plain Python and owns `bundle.json` and every build call. Agents
only ever edit page plans. `verify --strict`'s exit code is the termination signal,
so the loop has an external stopping criterion rather than a model's self-assessment.

`cost.py`, `editor.py` and the pure functions in `bundle.py` import **without** the
SDK on purpose, so the logic tests run offline. `paperskill/__init__.py` is lazy.
Keep it that way: anything pure that ends up behind the `agents` import stops being
testable, which already happened once with `collect_diagnostics`.

## Design rules, and why

**No raw coordinates.** A bbox changes only via `expand_bbox(item_id, side, points)`,
capped at 24 pt per step and clipped to the page.

**No silent drops.** Every item id present when a page loads must be accounted for
when it saves — present, merged (recorded), or `omit` with a reason.
`PageEditor.validate()` enforces it; `save()` refuses a page that fails.

**Refusals teach.** A rejected edit returns an explanation as the tool result rather
than raising, so the model learns what would be valid.

**The ops log is the only ground truth about what changed.** `mark_reviewed` appends
the applied operations automatically, because the model's prose notes cannot be
trusted (see below).

**One session per page.** Page plans are independent and the page image is the
expensive part of the context. Cache hit rates run 80–90% within a page.

## What verification does NOT catch

> Line coverage: checks whether each line's normalized alphanumeric text appears in
> its output page. **Ignores whitespace and formatting; does not establish sentence
> order** or punctuation fidelity. — the engine's own `vendor/references/review-plan.md`

So a package can pass `verify --strict` with figures misplaced, two-column prose
interleaved wrongly, or a sentence broken across a page with no `join_previous`.
Every character is present; the document is unreadable in order. That is why reading
order is step 2 of `PAGE_INSTRUCTIONS` with an explicit note that nothing downstream
will catch it. **Spend prompt-tuning effort there**, not on what the gate covers.

The cheaper-model run (`sk18-v2`) left Table 1 and Figs 3/4/6/7/8/10 inside sentences despite
the instruction. `hoist_floats()` now fixes this mechanically before every build: it moves a
float cluster (label + title + figure/table + caption) before the unterminated paragraph above
it, or after the sentence-ending item below a continuation (`join_previous` or a lower-case
opening). It refuses rather than guesses, is idempotent, and is tested against real page plans
in `tests/fixtures/sk18_float_pages.json`. It does not catch a float left after a bibliography
entry.

## Silent-loss checks, draft delivery, OCR, metrics (second batch, 8 papers)

Most defects in the 8-paper batch were content *missing* without any check noticing.

- **`accounting.py`** compares every native PDF word to the plan: inside a crop, present
  in retained text, inside furniture (header/footer/page number), or *unaccounted*.
  Bare numbers must match inside the item whose box holds them ("158" elsewhere on the
  page does not account for the 158 beside this item). `PageEditor.validate()` refuses a
  page whose `omit`s removed 4+ words found nowhere else (paper 15 p7: the reviewer
  omitted "duplicates" and lost "rainfall", "hepatitis"). "duplicate" is deliberately
  not an exempt reason.
- **`crops.py`** grows only the flagged side of a crop (row-aligned words left/right,
  column-aligned words above/below, words a crop edge slices through), via
  `expand_bbox`, max 80 pt a side, and never into another kept item. Paper 3 p27's row
  labels sat 50 pt left of the crop; its last rows still collide with a footnote and are
  reported, not guessed. Edge-ink checking was considered and left out: table rules
  touch the edge legitimately.
- **Draft delivery.** Strict verification failing no longer withholds the package.
  `coordinator` rebuilds into `--out` with the limitations in bundle.json `notes`
  (published inside the package; an extra root file would break the manifest check) and
  writes `<out>.LIMITATIONS.md` beside it. `final.status = delivered_draft`, exit code 3.
  Limitations are generated from diagnostics (`limitations.py`), never from model prose.
- **`--ocr-pages 7,12`** replaces those pages with Tesseract output in a copy of the
  input PDF before prepare (`ocr.py`); the vendored engine's OCR is whole-document only.
  Paper 15 p7's native layer is one word per line; OCR recovers it in order.
- **`metrics.py`** records per-paper counts in `report.json` and `run.py summarize
  runs/*-review` ranks papers (draft, unresolved crop, lost words, tables as images,
  float left, delivery fail) so a 150-paper run is triaged, not read.
- Other deterministic repairs added: `repair_builder_constraints` (heading `#` levels,
  `join_previous` placement, graphical-abstract name), `repair_degree_signs` (U+FFFD
  before C/F), `prune_stale_adjudications`, and `classify_missing_line` verdicts
  `dash_encoded` / `glyph_encoded`. `set_rows` refuses an image-fallback table without
  a reason; prompt step 7 asks for every table to be transcribed.


## Tables as images, and where the notes go (third batch)

- **`tabletx.py` / `tablesnap.py` / `tablefill.py`** — a deterministic fallback for every table
  the reviewer left without rows. `tabletx` renders the crop (turned upright if the text runs
  sideways, via `tablesnap.Frame`), asks a vision model for rows only, retries a cut-off table
  with the reported sides grown and a refusal in horizontal bands. `tablesnap` replaces tokens the
  PDF text layer does not carry with the unique closest PDF word (never numbers, never an
  ambiguous match, every change logged). The result is checked against the PDF's own words
  (coverage / precision) and graded accepted, partial, unchecked or rejected. `tablefill` writes
  rows through `PageEditor.set_rows` for every verdict, names anything but `accepted` in the
  notes, and keeps the image when a checked read covers under `table_min_coverage` (0.5) of the
  PDF's words, because a header-only CSV would replace the table. `--no-table-fallback` skips it.
  Offline evidence: `scripts/table_fallback_test.py`, `scripts/snap_test.py`, `table_tests/`.
  Findings: a table with rows ships as a CSV *instead of* its image; a PDF can store `+` as `þ`,
  so cells match joins of consecutive PDF words; word accounting counts everything inside a crop
  as carried, so title/footnote lines the model leaves out are recorded in the stamp and notes.
- **`notes.py`** — bundle.json `notes` (route/synthesis commentary, OCR, table fallback, draft
  limitations) no longer end `paper.md`. `Bundle._run` empties them for `build`, `verify` and
  `review-aid` (the engine fingerprints bundle.json at build time and verify fails with "Review
  input changed after build" if the file differs, so all three must see the same file), then
  restores them. They are written to `<out>.NOTES.md` with each page's review notes. Draft
  limitations still also go to `<out>.LIMITATIONS.md`. `--notes-in-package` restores the old way.

## Findings from the first real run (13-page Elsevier paper)

### Mangled comparison symbols — the most damaging defect

The extractor renders `>` as `N` and `<` as `b` before a number. The review restored
four of six and **deleted two**, turning `2.4->3.9-log10` (open upper bound) into
`2.4-3.9-log10` (closed range). Those were virus-removal values. Verification cannot
see it: the raw line counts as missing either way.

- `classify_missing_line()` classifies each missing line as
  `restored` / `symbol_dropped` / `absent` / `other`, comparing **the number the
  symbol qualifies**, not surrounding text — a review legitimately rewrites
  `log10` to `log₁₀` on the same line, so window comparison gives false alarms.
- `repair_dropped_symbols()` fixes them **deterministically**. Two agent passes with
  the rule stated explicitly still failed, and the correct answer is not a judgement
  call. It refuses rather than guesses when the anchor text is ambiguous, goes
  through `PageEditor`, and discloses itself in the page and source review notes.
- **`fonttools` is in `requirements.txt` for this reason.** It parses embedded
  CFF/Type1 encodings, which is where the corruption originates. Untested
  hypothesis worth checking on the next paper: installing it may prevent the
  mangling at source and make the repair a safety net rather than a necessity.
  Run one paper with and one without; compare the verdict counts.

### Review notes claim work that was not done

A page reported four text repairs; three were writes of byte-identical text. Only
`Editor: D. Barcelo` → `D. Barceló` was real — and it is length-preserving, so
`18->18 chars` looked identical to the phantom ones. Fixed by refusing a no-op
`set_markdown` and recording the actual diff (`@11 'o'->'ó'`) in the op string.
**Watch this as the corpus scales.** The prose reads convincingly; only the ops log
is reliable.

### Two review flags, both gating the build

- per page: `documents/<sid>/pages/page-NNNN.json`
- per source: **`bundle.json` → `sources[].reviewed`**, validated at
  `vendor/paper_bundle.py:327`, which also requires non-empty `review_notes`

Nothing was setting the second. `roll_up_source_review()` does it, generating the
notes as a factual roll-up (page count, preview DPI, operation counts from the op
log, asset DPI, reviewing model, limitations, and an explicit "not independently
certified" line). A source with no page plans — a workbook or opaque attachment —
is **never** claimed as reviewed; this harness reviews PDF pages only.

### `max_image_side` cancels a high `--asset-dpi`

The engine clamps a crop's longest side **after** rendering. Its default of 2800 was
silently cancelling `--asset-dpi 600`: a 464 pt figure renders at 3867 px and came
out 2800. Defaults here are `asset_dpi=600`, `max_image_side=4000`, giving 3867×1200
versus the engine default's 1160×360 — 3.3× linear, 11× pixels. Costs build time,
not tokens.

### Build output path rules

`vendor/reading_package.py:233` refuses an output dir that **exists**, is **inside**
the review dir, or **contains** it. All three bit. `check_paths()` validates before
`prepare` so a bad path fails in a second rather than after a paid review pass;
`fresh_dir()` gives each build attempt a new leaf; `clear_for_build()` moves an
existing package to `<name>.previous`.

### Exit 2 is a state, not a failure

`verify --strict` exits 2 for a pending verification state and still prints valid
JSON. Treating that as failure made `CmdResult.error` return the last stderr line,
which was an unrelated fontTools warning, and sent debugging the wrong way. `error`
is now empty whenever a status was parsed; `warnings` is separate.

### Adjudication needs real evidence, and then a rebuild

The review queue reports only a check name and a fingerprint — `detail` is null. The
actual differing number tokens are in `documents/<sid>/verification.json`. Given only
the queue, the adjudicator could only ever say "repair needed", and the loop spun 12
pages × 4 rounds. `collect_diagnostics()` joins the two. Most diagnostics on a journal
PDF are superscript tokenization (`10¹¹` is `1011` to one parser, `10` + `11` to the
other) and are genuinely parser-only.

Also: after writing adjudications you must **rebuild**, or they are never applied.
A page is re-reviewed at most once per run — a tokenization difference cannot be
re-read away, so retrying is a guaranteed spin.

## State

187 tests pass; pyflakes silent. Verified end to end on one paper: prepare, route,
extract, 13-page review, symbol repair, build at 600 dpi, verify, adjudication of 12
diagnostics. ~$1.35 and ~8 minutes for a first full pass; 80–90% cache hit.

Never exercised: a multi-source run (supplement PDF or `.xlsx`), `reuse-review`, OCR
mode, and a package whose retained text exceeds one context.

## If you change something

- Run `pyflakes` — three consecutive refactors leaked an undefined name or a stale
  import past review, which is why `tests/test_lint.py` exists.
- Tests pinning a vendored-format assumption (`test_queue.py`) were written from
  payloads copied out of real runs. If the engine's output shape changes they should
  fail; fix them from a new real run, not from guesses.
- Anything mechanical with one correct answer belongs in Python, not in a prompt.
  That lesson came from the symbol repair.
