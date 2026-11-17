"""All tunables and model/pricing config in one place."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict

try:                                  # stdlib from 3.11
    import tomllib
except ModuleNotFoundError:           # 3.10 and older
    try:
        import tomli as tomllib       # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None                # type: ignore[assignment]
                                      # only --config needs it; everything else works
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE.parent / "vendor"          # the three paper2skill scripts live here

# --- pricing -----------------------------------------------------------------
# USD per 1,000,000 tokens. VERIFY THESE against current OpenAI pricing before
# trusting a cost number: they are the single thing in this repo that silently
# goes stale. `paperskill pricing` prints the active table.
#
# cached_input applies to the cached portion of input tokens; the uncached
# remainder is billed at `input`.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-5":      {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
}


@dataclass
class Config:
    # models
    review_model: str = os.environ.get("PS_REVIEW_MODEL", "gpt-5")
    vision_model: str = os.environ.get("PS_VISION_MODEL", "gpt-5")
    # render DPI. The vendored defaults are preview 110 / assets 180; 180 is why
    # packaged figures come out ~1160px wide. 600 gives ~11x the pixels, which is
    # what downstream digitization needs.
    preview_dpi: int = 200          # page images the reviewer looks at
    asset_dpi: int = 600            # figure/table crops in the delivered package
    # The engine clamps every crop's longest side to this AFTER rendering at
    # asset_dpi, so the cap -- not the DPI -- decides the final pixel count.
    # Measured: a 464 pt wide journal figure at 600 dpi is 3867 px, which the
    # engine's default of 2800 silently clamps to 2800 (2.4x the 180 dpi
    # baseline instead of 3.3x). 4000 lets a full-width figure through intact.
    # The CLI accepts 720-6000.
    max_image_side: int = 4000
    jpeg_quality: int = 92
    transcribe_dpi: int = 400       # focused re-render for verbatim glyph repair

    # loop budgets
    # 20 was not enough for a dense page; measured caps on a references page
    # and a two-figure results page at 13 pages reviewed.
    max_turns_per_page: int = 28
    max_build_verify_rounds: int = 4
    max_transcribe_calls_per_page: int = 6
    bbox_nudge_max_pt: float = 24.0  # cap on a single expand_bbox step

    # behaviour
    require_reviewed: bool = True
    # tables the reviewer left as images are read by paperskill.tablefill; a read that
    # carries under this share of the PDF's own words keeps the image instead
    table_fallback: bool = True
    # False keeps the "Conversion notes" section out of the package; the notes are written
    # to <name>.NOTES.md beside it instead (paperskill.notes)
    notes_in_package: bool = False
    table_min_coverage: float = 0.5
    pricing: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_PRICING))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg = cls()
        if path:
            if tomllib is None:
                raise RuntimeError(
                    "Reading a --config TOML needs Python 3.11+ (for tomllib) or the "
                    "`tomli` package on older versions. Everything else works without it; "
                    "pass the options as CLI flags instead.")
            data = tomllib.loads(Path(path).read_text())
            pricing = data.pop("pricing", None)
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            if pricing:
                cfg.pricing.update(pricing)
        return cfg

    def as_dict(self) -> dict:
        return asdict(self)
