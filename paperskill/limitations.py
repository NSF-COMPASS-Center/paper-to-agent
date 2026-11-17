"""What a draft package is missing, stated in words a reader can act on.

A package that fails strict verification is still delivered, because withholding it
hides the 95% that is fine. The cost of delivering it is that a reader must be able
to tell what is not trustworthy. These functions turn the diagnostics the harness
already collected into that statement. Nothing here is written by a model: every
line comes from a verification result or a delivery finding.

Pure and SDK-free, like `delivery.py`, so it runs offline in tests.
"""
from __future__ import annotations

DRAFT_PREFIX = "DRAFT:"
MAX_NOTES = 12          # bundle notes are published in the package; keep them short
MAX_LINES = 3           # missing lines quoted per diagnostic
MAX_NUMBERS = 6         # differing numbers quoted per diagnostic
ACCOUNTING_MIN = 2      # a single stray token is extraction noise, not a limitation


def _clip(text: str, n: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _numbers(d: dict, key: str) -> list[str]:
    nums = d.get(key) or {}
    if isinstance(nums, dict):
        return [f"{k}×{v}" if isinstance(v, int) and v > 1 else str(k)
                for k, v in nums.items()]
    return [str(n) for n in nums]


def diagnostic_limitation(d: dict) -> dict:
    """One unresolved verification diagnostic, as a limitation."""
    page = d.get("page")
    parts: list[str] = []
    for ln in (d.get("missing_lines_classified") or [])[:MAX_LINES]:
        parts.append(f"line not found in output ({ln.get('verdict', 'unclassified')}): "
                     f"“{_clip(ln.get('raw', ''))}”")
    miss = _numbers(d, "numbers_in_source_not_in_output")
    extra = _numbers(d, "numbers_in_output_not_in_source")
    if miss:
        parts.append("numbers in the source but not the output: "
                     + ", ".join(miss[:MAX_NUMBERS]))
    if extra:
        parts.append("numbers in the output but not the source: "
                     + ", ".join(extra[:MAX_NUMBERS]))
    return {"kind": "verification", "source": d.get("source_id", ""), "page": page,
            "check": d.get("check", ""),
            "detail": "; ".join(parts) or "the verifier reported a difference"}


def accounting_limitation(a: dict) -> dict:
    causes = ", ".join(f"{n} {c}" for c, n in sorted((a.get("causes") or {}).items()))
    bits = []
    if a.get("words"):
        bits.append(f"words in the PDF but not in the package ({causes}): "
                    + ", ".join(a["words"][:8]))
    if a.get("garbled_items"):
        bits.append("the PDF text layer is garbled (one word per line); the text of "
                    + ", ".join(a["garbled_items"][:4]) + " may be unreliable and could "
                    "need OCR")
    return {"kind": "missing_text", "source": a.get("source", ""), "page": a.get("page"),
            "check": "word_accounting", "detail": "; ".join(bits) or a.get("error", "")}


def draft_limitations(diagnostics: list[dict], delivery: list[dict] | None = None,
                      extra: list[str] | None = None,
                      accounting: list[dict] | None = None) -> list[dict]:
    """Every known reason this package is a draft, most specific first."""
    out = [diagnostic_limitation(d) for d in diagnostics]
    out += [accounting_limitation(a) for a in (accounting or [])
            if sum((a.get("causes") or {}).values()) >= ACCOUNTING_MIN]
    for f in (delivery or []):
        if f.get("status") == "fail":
            out.append({"kind": "delivery", "source": "", "page": None,
                        "check": f.get("check", ""), "detail": str(f.get("detail", ""))})
    for text in (extra or []):
        out.append({"kind": "run", "source": "", "page": None, "check": "",
                    "detail": str(text)})
    return out


def _where(lim: dict) -> str:
    bits = []
    if lim.get("source"):
        bits.append(lim["source"])
    if lim.get("page"):
        bits.append(f"page {lim['page']}")
    return ", ".join(bits)


def bundle_notes(limitations: list[dict]) -> list[str]:
    """Lines for bundle.json `notes`, which the builder publishes in the package."""
    if not limitations:
        return []
    notes = [f"{DRAFT_PREFIX} strict verification did not pass; {len(limitations)} "
             "unresolved issue(s) are listed below. Treat the affected passages as "
             "unverified and check them against the source PDF."]
    for lim in limitations[:MAX_NOTES]:
        where = _where(lim)
        notes.append(f"{DRAFT_PREFIX} {where + ': ' if where else ''}{lim['detail']}")
    if len(limitations) > MAX_NOTES:
        notes.append(f"{DRAFT_PREFIX} {len(limitations) - MAX_NOTES} further issue(s) are "
                     "listed in LIMITATIONS.md beside the package.")
    return notes


def merge_notes(existing: list[str], limitations: list[dict]) -> list[str]:
    """Replace any earlier DRAFT lines, so a resumed run does not stack duplicates."""
    kept = [n for n in existing if not str(n).startswith(DRAFT_PREFIX)]
    return kept + bundle_notes(limitations)


def render_markdown(name: str, limitations: list[dict], reason: str = "") -> str:
    lines = [f"# {name}: DRAFT, limitations", "",
             "This package was built but did **not** pass strict verification. It is "
             "delivered so the verified remainder is usable; the passages below are "
             "not verified and should be checked against the source PDF.", ""]
    if reason:
        lines += [f"Why the run stopped: {reason}", ""]
    if not limitations:
        lines.append("No individual issue was recorded; see report.json.")
    for i, lim in enumerate(limitations, 1):
        where = _where(lim)
        lines.append(f"{i}. **{lim['kind']}**"
                     + (f" ({where})" if where else "")
                     + (f", check `{lim['check']}`" if lim.get("check") else "")
                     + f": {lim['detail']}")
    lines.append("")
    return "\n".join(lines)
