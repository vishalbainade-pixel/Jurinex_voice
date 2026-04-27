"""Audio codec helpers for the Twilio Media Streams ↔ Gemini bridge.

Twilio Media Streams send μ-law (G.711) at 8kHz, base64-encoded, in 20ms frames.
Gemini Live expects 16-bit PCM at 16kHz on input, returns 16-bit PCM at 24kHz.

We rely on stdlib ``audioop`` for both companding and resampling. ``audioop``
is deprecated as of Python 3.13 — when you migrate, swap in a third-party
library (e.g. ``audioop-lts`` or ``soxr``).
"""

from __future__ import annotations

import audioop  # noqa: DEP001 — deprecated in 3.13; swap out then.
import base64

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TWILIO_FRAME_BYTES = 160          # 20ms of μ-law @ 8kHz
TWILIO_SAMPLE_RATE = 8000
GEMINI_INPUT_SAMPLE_RATE = 16000
GEMINI_OUTPUT_SAMPLE_RATE = 24000

# ---------------------------------------------------------------------------
# Twilio frame encode/decode
# ---------------------------------------------------------------------------


def decode_twilio_payload(payload_b64: str) -> bytes:
    """Twilio media payload → raw μ-law 8kHz bytes."""
    return base64.b64decode(payload_b64)


def encode_twilio_payload(audio_bytes: bytes) -> str:
    """Raw μ-law 8kHz bytes → base64 string for a Twilio media event."""
    return base64.b64encode(audio_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# Caller (Twilio) → Gemini  :  μ-law 8k  →  PCM16 16k
# ---------------------------------------------------------------------------


def mulaw8k_to_pcm16_16k(mulaw_bytes: bytes) -> bytes:
    """Decode μ-law 8kHz to PCM16 then resample to 16kHz (mono)."""
    if not mulaw_bytes:
        return b""
    pcm16_8k = audioop.ulaw2lin(mulaw_bytes, 2)  # 2 bytes/sample = 16-bit
    pcm16_16k, _ = audioop.ratecv(pcm16_8k, 2, 1, 8000, 16000, None)
    return pcm16_16k


# ---------------------------------------------------------------------------
# Gemini → Caller (Twilio)  :  PCM16 24k  →  μ-law 8k
# ---------------------------------------------------------------------------


class Pcm24kToMulaw8k:
    """Stateful resampler — preserves ratecv state across chunks."""

    def __init__(self) -> None:
        self._state = None  # ratecv carry-state

    def convert(self, pcm16_24k: bytes) -> bytes:
        if not pcm16_24k:
            return b""
        pcm16_8k, self._state = audioop.ratecv(
            pcm16_24k, 2, 1, 24000, 8000, self._state
        )
        return audioop.lin2ulaw(pcm16_8k, 2)


def chunk_mulaw_for_twilio(mulaw_bytes: bytes) -> list[bytes]:
    """Split a μ-law buffer into 20ms (160-byte) frames Twilio expects."""
    return [
        mulaw_bytes[i : i + TWILIO_FRAME_BYTES]
        for i in range(0, len(mulaw_bytes), TWILIO_FRAME_BYTES)
        if mulaw_bytes[i : i + TWILIO_FRAME_BYTES]
    ]
