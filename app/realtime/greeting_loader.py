"""Pre-load + pre-resample the eager greeting WAV to μ-law 8kHz at startup.

Cached in memory so each call can stream it through the Twilio Media
Stream WS instantly (no <Play> verb, no extra HTTP fetch). This lets
the Gemini Live session warm up *during* greeting playback instead of
sequentially after it.
"""

from __future__ import annotations

import audioop  # noqa: DEP001 — deprecated in 3.13
import wave
from pathlib import Path

from app.config import settings
from app.observability.logger import log_dataflow

_greeting_mulaw: bytes | None = None
_greeting_duration_seconds: float = 0.0


def _resolve_local_path() -> Path | None:
    """Return the local filesystem path of the configured greeting file."""
    url = settings.eager_greeting_audio_url.strip()
    if not url:
        return None
    if url.lower().startswith(("http://", "https://")):
        # Remote URL — we can't pre-load it, fall back to <Play>.
        return None
    # Treat as a path relative to the FastAPI app dir. /static/... maps
    # to app/static/...
    rel = url.lstrip("/")
    candidate = Path(__file__).parent.parent / rel
    return candidate if candidate.exists() else None


def load_greeting() -> bool:
    """Decode + resample the greeting WAV to μ-law 8kHz; cache in memory.

    Idempotent. Returns True on success.
    """
    global _greeting_mulaw, _greeting_duration_seconds

    if not settings.eager_greeting_enabled:
        log_dataflow("greeting.load.skipped", "eager greeting disabled")
        return False

    path = _resolve_local_path()
    if path is None:
        log_dataflow(
            "greeting.load.skipped",
            f"no local file resolved (url={settings.eager_greeting_audio_url!r})",
            level="debug",
        )
        return False

    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            sample_width = w.getsampwidth()
            pcm = w.readframes(w.getnframes())
    except Exception as exc:
        log_dataflow("greeting.load.error", f"wave.open failed: {exc}", level="warning")
        return False

    # Coerce to mono 16-bit.
    if channels == 2:
        pcm = audioop.tomono(pcm, sample_width, 0.5, 0.5)
    if sample_width != 2:
        pcm = audioop.lin2lin(pcm, sample_width, 2)

    # Resample to 8kHz.
    if rate != 8000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)

    # Encode to μ-law for Twilio.
    mulaw = audioop.lin2ulaw(pcm, 2)

    _greeting_mulaw = mulaw
    _greeting_duration_seconds = len(mulaw) / 8000.0
    log_dataflow(
        "greeting.load.ok",
        f"path={path.name} bytes={len(mulaw)} duration={_greeting_duration_seconds:.2f}s",
    )
    return True


def get_greeting_mulaw() -> bytes | None:
    return _greeting_mulaw


def get_greeting_duration() -> float:
    return _greeting_duration_seconds
