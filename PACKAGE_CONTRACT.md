# Package contract: paperskill → sciex

What `paperskill_agent` promises a package looks like, and what `sciex_agent_v1/extract/package.py`
(stage 0) accepts. If you change one side, change this file and the other side's tests
(`tests/test_delivery.py::test_sciex_compat_*` here, `tests/test_package.py` and
`tests/test_image_tables.py` there).

## Layout

```
dist/<name>/                    the package (the builder's manifest forbids extra files inside it)
  SKILL.md
  references/paper.md           the paper; the only file stage 0 parses for assets and sections
  references/index.md           navigation table: | Exact heading | Look here for |
  references/supplement.md      "No corresponding materials were supplied." stub, or real text (> 200 bytes)
  assets/{figure,table,supp_figs,supp_table}/…
dist/<name>.NOTES.md            conversion notes sidecar (see below)
dist/<name>.LIMITATIONS.md      draft packages only; human-readable, not read by sciex
dist/INDEX.json                 per-package status (reviewed | reviewed_with_limitations | delivered_draft)
```

## Assets

- Link form in `paper.md`: `[label](../assets/<folder>/<id>.<ext>)`.
- `<folder>` ∈ `figure`, `table`, `supp_figs`, `supp_table`. `<ext>` ∈ `jpg`, `jpeg`, `png`, `csv`.
  `<id>` is `[A-Za-z0-9_.-]+`. Supplementary assets are inventoried exactly like main ones.
- **Kind comes from the label, then the id, then the folder.** A table that could only be kept as a
  picture of a PDF page may sit in `assets/figure/` (`figure-scale-legend.jpg`, label "Table on PDF
  page 19 (5)"); the extractor treats it as a table.
- **Format comes from the extension.** A table is `csv` (machine-readable) or an image (`jpg`/`png`,
  kept when the converter could not transcribe it to a coverage-checked CSV). An image table is never
  read as text; it is carried to the review page as an image and listed in the gates
  (`table_image_only`) and in the batch spreadsheet (`image tables` sheet).
- `formula-*` files (display equations stored as pictures) are not assets and are ignored.
- `graphical-abstract` is ignored.
- Every linked asset must exist on disk and every file on disk must be linked (stage 0 fails loudly
  otherwise).
- A caption is the next non-empty block after the link, or the block before it for a table whose markdown
  rendering follows the link. If neither exists the label stands in for the caption.
- Citation keys: a paper cites "Figure 3", "Table S4", "S1 Fig."; the extractor maps those to assets from
  the id (`figure-3`, `table-s4`, `supplementary-table-8`) or the label. Positional ids
  (`table-p0025-001`) cannot be cited, so they get the section the link sits in.

## Text

- One `#` heading (the title); sections are `##` / `###` / `####`.
- A supplement merged into `paper.md` after the bibliography should have its own heading. If it does not,
  stage 0 splits it off the `References` section at the first paragraph opening with
  "Supporting / Supplementary / Supplemental …" (or the first of two or more asset links) and keeps it as
  the section "Supporting information (follows References)".
- Stage 0 drops these sections, compared after removing numbering and case:
  References, Introduction, Acknowledg(e)ments, Update, Graphical abstract, Article info, and "Appendix A…".
  Corrigendum and anything else are kept.

## Notes

The converter's commentary (unit corrections, transcription caveats, table-fallback verdicts, `DRAFT:`
limitations, repairs) is **not** in `paper.md` for current builds. It is published in
`dist/<name>.NOTES.md` with `## Conversion notes` (bullets) and `## Page review notes`.

- sciex reads `## Conversion notes` into the stage-1 `PACKAGE NOTES` block, so a published unit correction
  is applied. `## Page review notes` is shown on the review page only.
- Older builds have a `## Conversion notes` section inside `paper.md` and no sidecar; sciex falls back to it
  (`package.json` → `notes_source`: `sidecar` | `paper.md` | `none`).
- A bullet starting `DRAFT` marks an unverified passage and raises the `draft_package` gate; a bullet
  mentioning the table fallback raises `table_unverified`.

## Checks

paperskill: `inspect_package` → `sciex_compat` warns on links outside this grammar, label/folder
disagreement, and tables kept as images.
sciex: stage 0 (`package.py`), gates `table_image_only`, `draft_package`, `table_unverified`,
`notes_missing`.
