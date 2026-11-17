"""Delivery inspection. No API key, no network.

The skill requires this step ("Before delivery, inspect the generated index...")
and it was never implemented, which is how a package with a two-row navigation
table shipped as a completed conversion.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paperskill.delivery import headings, inspect_package, nav_rows, summarize  # noqa: E402

GOOD_INDEX = """# Paper navigation

## Main paper — [paper.md](paper.md)

| Exact heading | Look here for |
| --- | --- |
| Abstract | Headline removal ratios and the coagulants tested |
| 1. Introduction | Background on enteric viruses and why MS2 is used |
| 2.1. Source water and coagulants | The 13 source waters; Table 1 is here |
| 3.4. Effect of virucidal activity | PFU versus PCR ratios; Figure 9 |
| 4. Conclusions | The five numbered conclusions |
"""

BAD_INDEX = """# Paper navigation

## Main paper — [paper.md](paper.md)

| Exact heading | Look here for |
| --- | --- |
| Investigation of enteric adenovirus and poliovirus removal |  |
| N. Shirasaki, T. Matsushita, Y. Matsui |  |
"""

GOOD_PAPER = """# Investigation of enteric adenovirus and poliovirus removal

## Abstract

Text. [Figure 1](../assets/figure/figure-1.jpg)

## 1. Introduction

Text.

### 2.1. Source water and coagulants

[Table 1](../assets/table/table-1.csv)

## 3.4. Effect of virucidal activity

## 4. Conclusions
"""

BAD_PAPER = """# shirasaki-18

# Science of the Total Environment

## Investigation of enteric adenovirus and poliovirus removal

# 1. Introduction

# 2. Materials and methods

# 3.1. Virus removal

[Figure 1](../assets/figure/figure-p0001-002.jpg)
"""


def _pkg(tmp_path, paper, index, assets, notes=None):
    import json
    p = tmp_path / "pkg"
    (p / "references").mkdir(parents=True)
    (p / "SKILL.md").write_text("# skill")
    (p / "references" / "paper.md").write_text(paper)
    (p / "references" / "index.md").write_text(index)
    for rel in assets:
        f = p / "assets" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    w = tmp_path / "review"
    w.mkdir(exist_ok=True)
    (w / "bundle.json").write_text(json.dumps({"notes": notes or []}))
    return p, w


def test_a_good_package_passes(tmp_path):
    pkg, w = _pkg(tmp_path, GOOD_PAPER, GOOD_INDEX,
                  ["figure/figure-1.jpg", "table/table-1.csv"],
                  notes=["Supplement was not supplied."])
    fails, warns, _ = summarize(inspect_package(pkg, w))
    assert fails == 0


def test_level_one_sections_are_caught(tmp_path):
    pkg, w = _pkg(tmp_path, BAD_PAPER, BAD_INDEX, ["figure/figure-p0001-002.jpg"])
    f = {x["check"]: x for x in inspect_package(pkg, w)}
    assert f["heading_levels"]["status"] == "fail"
    assert "builder owns" in f["heading_levels"]["detail"]


def test_a_two_row_index_is_caught(tmp_path):
    pkg, w = _pkg(tmp_path, BAD_PAPER, BAD_INDEX, ["figure/figure-p0001-002.jpg"])
    f = {x["check"]: x for x in inspect_package(pkg, w)}
    assert f["index_rows"]["status"] == "fail"


def test_blank_purpose_column_is_caught(tmp_path):
    index = GOOD_INDEX.replace("| Headline removal ratios and the coagulants tested |", "|  |")
    for old in ["Background on enteric viruses and why MS2 is used",
                "The 13 source waters; Table 1 is here",
                "PFU versus PCR ratios; Figure 9", "The five numbered conclusions"]:
        index = index.replace(old, "")
    pkg, w = _pkg(tmp_path, GOOD_PAPER, index,
                  ["figure/figure-1.jpg", "table/table-1.csv"], notes=["n"])
    f = {x["check"]: x for x in inspect_package(pkg, w)}
    assert f["index_purposes"]["status"] == "fail"
    assert "curated `navigation`" in f["index_purposes"]["detail"]


def test_unlabelled_assets_are_caught(tmp_path):
    pkg, w = _pkg(tmp_path, GOOD_PAPER, GOOD_INDEX,
                  ["figure/figure-p0005-002.jpg", "table/table-p0003-017.csv"], notes=["n"])
    f = {x["check"]: x for x in inspect_package(pkg, w)}
    assert f["asset_names"]["status"] == "fail"
    assert "figure-p0005-002" in f["asset_names"]["detail"]


def test_missing_notes_are_caught(tmp_path):
    pkg, w = _pkg(tmp_path, GOOD_PAPER, GOOD_INDEX,
                  ["figure/figure-1.jpg", "table/table-1.csv"], notes=[])
    f = {x["check"]: x for x in inspect_package(pkg, w)}
    assert f["notes"]["status"] == "fail"


def test_missing_required_file_short_circuits(tmp_path):
    pkg, w = _pkg(tmp_path, GOOD_PAPER, GOOD_INDEX, ["figure/figure-1.jpg"], notes=["n"])
    (pkg / "references" / "index.md").unlink()
    f = inspect_package(pkg, w)
    assert f[0]["check"] == "required_files" and f[0]["status"] == "fail"


def test_headings_ignores_fenced_blocks():
    md = "## Real\n\n```\n## Not a heading\n```\n\n### Also real"
    assert headings(md) == [(2, "Real"), (3, "Also real")]


def test_nav_rows_skips_boilerplate():
    rows = nav_rows(GOOD_INDEX)
    assert len(rows) == 5
    assert rows[0][0] == "Abstract" and rows[0][1].startswith("Headline")


def test_sciex_compat_ok_for_contract_links(tmp_path):
    pkg, w = _pkg(tmp_path, GOOD_PAPER, GOOD_INDEX, ["figure/figure-1.jpg", "table/table-1.csv"], notes=["n"])
    f = {x["check"]: x for x in inspect_package(pkg, w)}
    assert f["sciex_compat"]["status"] == "ok"


def test_sciex_compat_warns_on_image_tables_mismatch_and_foreign_links(tmp_path):
    paper = GOOD_PAPER + (
        "\n[Table S6](../assets/supp_table/table-s6.jpg)\n\n"
        "[Table on PDF page 19 (5)](../assets/figure/figure-scale-legend.jpg)\n\n"
        "[Plot](../assets/extra/plot.gif)\n")
    pkg, w = _pkg(tmp_path, paper, GOOD_INDEX,
                  ["figure/figure-1.jpg", "table/table-1.csv", "supp_table/table-s6.jpg",
                   "figure/figure-scale-legend.jpg"], notes=["n"])
    f = {x["check"]: x for x in inspect_package(pkg, w)}["sciex_compat"]
    assert f["status"] == "warn"
    assert "assets/extra/plot.gif" in f["detail"]
    assert "label and folder disagree" in f["detail"]
    assert "table-s6" in f["detail"] and "kept as images" in f["detail"]
