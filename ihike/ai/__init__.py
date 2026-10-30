"""iHike AI features that run inside the Space, on its CPU.

- transcribe():     speech -> text with Whisper (faster-whisper, int8)
- identify():       photo -> plant or animal, with BioCLIP (a CLIP model trained on the tree of life)
- understand():     free text -> search parameters (rules, refined by an LLM when one is available)

Models load on first use and stay in memory.
"""

from __future__ import annotations

import importlib.util

from .parsing import parse_request, understand
from .species import NOT_A_SPECIES, SPECIES, identify
from .speech import transcribe

__all__ = ["NOT_A_SPECIES", "SPECIES", "available", "identify", "parse_request", "transcribe", "understand"]


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def available() -> dict:
    """Which in-Space features have their Python packages installed."""
    return {"voice": _installed("faster_whisper"), "species": _installed("open_clip") and _installed("torch")}
