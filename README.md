# paperskill-agent

Converts a research PDF into a reviewed, verified skill package, driven through
the **OpenAI Agents SDK** instead of Claude Code. Reports the exact cost of every
conversion.

It wraps the `paper2skill` engine unchanged (vendored in `vendor/`): three
standalone scripts, four pip dependencies, nothing Claude-specific. The engine
does extraction, building and verification. This repo supplies the review loop
that the engine expects an agent to perform.

---

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=...
```

`uv` is used automatically for the vendored scripts if it is on PATH (it resolves
their inline PEP-723 deps). Without `uv`, the pinned `pymupdf` / `pymupdf4llm` /
`pypdf` / `pillow` in `requirements.txt` cover it. `pymupdf` is needed by this
package too, for high-resolution region rendering.

## Use

```bash
# one PDF
python run.py convert paper.pdf --name example-paper --out dist/example-paper

# main + supplement + a spreadsheet of tables
python run.py convert main.pdf supp.pdf tables.xlsx \
    --name example-paper --title 'Full paper title' --main main.pdf \
    --work runs/example-review --out dist/example-paper --concurrency 4

python run.py convert paper.pdf --name x --out dist/x --pages 3,4,7-9  # subset, for debugging
python run.py convert paper.pdf --name x --out dist/x --resume         # keep prepare/extract
python run.py cost runs/example-review                                 # re-print the cost report
python run.py pricing                                                  # show the price table
```

Exit code is 0 when strict verification passed, 2 otherwise.

## Cost tracking

Every model call in the system goes through `Ledger.record`, so the total is
complete by construction rather than by remembering to instrument a call site.

Two files land in `<work>/agent-run/`:

- `cost.jsonl` — one append-only line per API call: stage, page, model, input,
  cached, output and reasoning tokens, USD, seconds.
- `cost.json` — the rolled-up summary, also broken down by stage, model and page.

A report prints at the end of every run:

```
====================================================================
  COST  $0.9834   71 calls   437s wall
====================================================================
  input   881,600  (cached 510,800 = 58%)
  output   45,600  (reasoning 27,450)

  stage            calls     input   output      usd
  --------------------------------------------------
  page                69   864,400   43,900   0.9449
  adjudicate           1    11,000      800   0.0217
  bundle               1     6,200      900   0.0168

  most expensive pages: p11 $0.119, p4 $0.114, p2 $0.104, p9 $0.093, p5 $0.085
====================================================================
```

(Illustrative figures, not a measured run.) Three things worth knowing:

- **Cached input is billed at the cached rate.** Reported `input_tokens` include
  cached ones, so the ledger splits them: `(input - cached) x input_rate +
  cached x cached_rate + output x output_rate`. Getting this wrong overstates
  cost by several times on a multi-turn page.
- **Per-turn, not per-page.** A page that runs away shows which turn did it.
- **An unpriced model is flagged, never silently free.** A model missing from the
  table counts as $0 *and* prints a `!! NO PRICING` warning with its name. Dated
  snapshots (`gpt-5-2026-03-01`) price by longest-prefix match.

**Verify the price table before trusting a number** — it is the one thing here
that goes stale silently. `python run.py pricing` shows what is active; override
with a `[pricing]` block in a `--config` TOML (see `config.example.toml`).

## How it runs

| stage | model? | agent? | what happens |
|---|---|---|---|
| prepare | no | no | `paper_bundle.py prepare` snapshots sources, writes `inventory.json` |
| route | **yes** | no | one structured call assigns roles/titles/asset names and writes the published `notes` |
| extract | no | no | per-page plans, evidence and previews |
| **review** | **yes** | **yes** | one fresh agent session per page, with tools |
| build | no | no | `build --require-reviewed` |
| verify | no | no | `verify --strict` — **exit 0 or 2** |
| adjudicate | yes | no | classifies parser-only diagnostics; anything else routes back to page review |

The build/verify/adjudicate loop repeats up to `max_build_verify_rounds`. The
coordinator is plain Python and owns `bundle.json` and every build call; agents
only ever touch page plans and adjudications.

`verify --strict`'s exit code is the termination signal. The loop has an external
stopping criterion rather than a model's opinion of its own work — which is the
main reason this task suits an agent at all.

## Design rules worth keeping

**No raw coordinates.** A bbox changes only through `expand_bbox(item_id, side,
points)`, capped per step at 24 pt and clipped to the page. The model names a side
and a distance; it never emits a rectangle.

**No silent drops.** Every item id present when a page loads must still be
accounted for when it saves — present, merged into another item (recorded), or
turned into an `omit` with a specific reason. `PageEditor.validate()` enforces it
and `save()` refuses a page that fails. `test_editor.py` pins this.

**Refusals teach.** A rejected edit returns an explanation as the tool result
instead of raising, so the model learns what would be valid. Every refusal message
says what was wrong *and* what would work.

**Review notes must be specific.** `mark_reviewed` rejects anything under 25
characters, and the applied-ops list is appended automatically, so the notes
record what actually changed rather than what the model says it did.

**One session per page.** Page plans are independent and the page image is the
expensive part of the context. A session carried across pages resends every
earlier image on every later request.

## What it does NOT catch

**Verification does not check reading order.** From the engine's own reference:
*"Line coverage ... Ignores whitespace and formatting; does not establish sentence
order or punctuation fidelity."*

So a package can pass `verify --strict` with figures in the wrong place, two-column
prose interleaved wrongly, or a sentence broken across a page with no
`join_previous`. Every character is present; the document is unreadable in order.
That is why `PAGE_INSTRUCTIONS` puts reading order first and says so explicitly,
and why the per-page `review_notes` are worth reading on the first few papers.
**Spend your prompt-tuning effort there**, not on the parts the gate already covers.

## DPI

The engine's defaults are `--preview-dpi 110` and `--dpi 180`; both cap at 600.
That 180 is why packaged figures come out ~1160 px wide. This repo defaults to:

| | default here | engine default |
|---|---|---|
| page previews the reviewer sees | 200 | 110 |
| figure/table crops in the package | **600** | 180 |
| focused re-render for glyph repair | 400 | n/a |

600 dpi is ~3.3x linear and ~11x the pixels, measured on a journal figure
(1160x360 → 3867x1200). If anything downstream digitizes those figures, this is
the single highest-value setting in the file. Raising it costs build time and disk,
not tokens.

## Layout

```
run.py                  CLI: convert / cost / pricing
paperskill/
  config.py             models, DPI, budgets, price table
  cost.py               Ledger -- all token and cost accounting
  bundle.py             typed wrappers around the vendored CLI
  editor.py             constrained page-plan edits + integrity rules
  tools.py              the 15 tools the page reviewer gets
  vision.py             image encoding, region render, verbatim transcription
  prompts.py            the instruction strings
  review.py             per-page agent session
  coordinator.py        prepare -> route -> extract -> review -> build -> verify
vendor/                 paper2skill engine, unmodified, + its two reference docs
tests/                  29 tests, no API key and no network required
```

`cost.py` and `editor.py` import without the SDK on purpose, so CI can run the
logic tests offline. `paperskill/__init__.py` imports the agent modules lazily.

## Tests

```bash
pytest tests/ -q        # 29 passed
```

Covered: cached-token pricing splits, prefix matching for dated models, unpriced
models flagged, `cost.jsonl` reconciling exactly against the summary, per-turn
recording with an aggregate fallback, every `PageEditor` invariant and refusal,
and `review-queue.json` parsing pinned to the shape `review-aid` really emits.

## Known rough edges

- `route_sources` merges into `bundle.json` by matching source ids. If a future
  engine version renames that key the merge silently no-ops — `stages.route` in
  `report.json` shows what it decided, so check it on your first run.
- Adjudication defaults to `repair_needed`. That is intentional (a wrong
  adjudication hides a real error behind a clean verification) but it means a
  paper with many genuine parser-only differences may burn re-review rounds.
  Watch `stages.adjudications` and raise `max_build_verify_rounds` if needed.
- Concurrency is per page within one paper. Running several papers at once is
  fine, but give each its own `--work`; nothing is shared and nothing should be.
