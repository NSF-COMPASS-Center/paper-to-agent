"""Paper -> reviewed skill package, driven through the OpenAI Agents SDK.

`cost` and `editor` are deliberately importable without the SDK or an API key, so
they can be unit-tested in CI with no network. The agent-facing modules are
imported lazily on first attribute access.
"""
from .config import Config, DEFAULT_PRICING
from .cost import CallCost, Ledger, extract_usage
from .editor import EditError, PageEditor

_LAZY = {
    "Bundle": ".bundle", "CmdResult": ".bundle",
    "convert": ".coordinator",
    "review_page": ".review",
    "PageContext": ".tools", "PAGE_TOOLS": ".tools",
}

__all__ = ["Config", "DEFAULT_PRICING", "CallCost", "Ledger", "extract_usage",
           "EditError", "PageEditor", *_LAZY]


def __getattr__(name):
    if name in _LAZY:
        from importlib import import_module
        return getattr(import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
