"""Speech to text with Whisper (faster-whisper, int8, CPU)."""

from __future__ import annotations

import os
import threading

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base").strip()

_whisper = None
_whisper_lock = threading.Lock()


def transcribe(path: str) -> str:
    """Transcribe a short voice note (any language Whisper knows)."""
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel

            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, _info = _whisper.transcribe(path, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
