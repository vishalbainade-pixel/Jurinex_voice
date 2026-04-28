"""In-memory call audio recorder + timeline-aligned mixer.

Captures both audio sides as they flow through the realtime layer:
  - caller side: μ-law @ 8kHz, continuous from Twilio
  - agent side:  PCM16 @ 24kHz, bursty (only when Gemini speaks)

At call end ``encode_mixed_wav()`` produces a single PCM16 16kHz mono WAV
with both sides time-aligned on a shared timeline:
  - the caller is laid down continuously from t=0
  - each agent chunk is placed at the moment it actually arrived from Gemini
    so gaps between turns are preserved as silence
The two are then sample-summed (audioop.add clips automatically).
"""

from __future__ import annotations

import audioop  # noqa: DEP001 — deprecated in 3.13; swap to audioop-lts then.
import io
import time
import wave
from datetime import datetime


CALLER_SAMPLE_RATE = 8000
AGENT_SAMPLE_RATE = 24000
MIX_SAMPLE_RATE = 16000


class CallRecorder:
    """Buffers caller + agent audio for the duration of a single call."""

    def __init__(
        self,
        *,
        call_sid: str | None,
        started_at: datetime,
        enabled: bool,
    ) -> None:
        self.call_sid = call_sid or "unknown"
        self.started_at = started_at
        self.enabled = enabled

        # Caller is continuous μ-law; the buffer length defines call duration.
        self._caller_mulaw = bytearray()
        # Agent is bursty — store each chunk with its arrival time relative
        # to recorder creation, so the mixer can place it on the timeline.
        self._agent_chunks: list[tuple[float, bytes]] = []

        # Monotonic anchor — every agent chunk is tagged relative to this.
        self._t0 = time.monotonic()

    # ------------------------------------------------------------------
    # Capture (called from the realtime hot path — must be cheap)
    # ------------------------------------------------------------------

    def add_caller_audio(self, mulaw_bytes: bytes) -> None:
        if self.enabled and mulaw_bytes:
            self._caller_mulaw.extend(mulaw_bytes)

    def add_agent_audio(self, pcm24k_bytes: bytes) -> None:
        if self.enabled and pcm24k_bytes:
            self._agent_chunks.append(
                (time.monotonic() - self._t0, bytes(pcm24k_bytes))
            )

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def caller_seconds(self) -> float:
        return len(self._caller_mulaw) / CALLER_SAMPLE_RATE

    @property
    def agent_seconds(self) -> float:
        # Sum of agent audio actually spoken (not the timeline gaps)
        total_bytes = sum(len(chunk) for _, chunk in self._agent_chunks)
        return total_bytes / (AGENT_SAMPLE_RATE * 2)

    @property
    def total_seconds(self) -> float:
        """Total call duration to render — max(caller end, last agent end)."""
        caller_end = self.caller_seconds
        agent_end = 0.0
        for offset, chunk in self._agent_chunks:
            agent_end = max(agent_end, offset + len(chunk) / (AGENT_SAMPLE_RATE * 2))
        return max(caller_end, agent_end)

    def has_audio(self) -> bool:
        return bool(self._caller_mulaw) or bool(self._agent_chunks)

    # ------------------------------------------------------------------
    # Mix → single WAV
    # ------------------------------------------------------------------

    # Gap (in seconds) between two consecutive agent chunks above which we
    # treat them as belonging to *different* turns. Gemini streams audio
    # faster than realtime within a single turn, so chunks normally arrive
    # ~50-200 ms apart; a half-second gap is a safe boundary.
    _TURN_GAP_SECONDS = 0.5

    def encode_mixed_wav(self) -> bytes | None:
        """Produce one PCM16 16 kHz mono WAV with both sides on a shared timeline.

        Caller audio is laid down continuously from t=0. Agent audio is
        rendered turn-by-turn: chunks within the same turn are concatenated
        back-to-back starting from the turn's first-chunk arrival time;
        between turns the silence is preserved.
        """
        if not self.has_audio():
            return None

        # First, render the agent timeline so we know how long it is.
        agent_buf, agent_total_seconds = self._render_agent_timeline()
        caller_seconds = self.caller_seconds
        total_seconds = max(caller_seconds, agent_total_seconds)
        if total_seconds <= 0:
            return None
        target_bytes = int(total_seconds * MIX_SAMPLE_RATE) * 2  # 16-bit

        # 1. Caller timeline — μ-law 8k → PCM16 8k → PCM16 16k
        if self._caller_mulaw:
            caller_pcm8k = audioop.ulaw2lin(bytes(self._caller_mulaw), 2)
            caller_pcm16k, _ = audioop.ratecv(
                caller_pcm8k, 2, 1, CALLER_SAMPLE_RATE, MIX_SAMPLE_RATE, None
            )
            caller_buf = _fit_to_size(caller_pcm16k, target_bytes)
        else:
            caller_buf = b"\x00" * target_bytes

        # 2. Pad/trim agent buffer to match.
        agent_buf = _fit_to_size(bytes(agent_buf), target_bytes)

        # 3. Mix (sample-wise sum with clipping).
        mixed = audioop.add(caller_buf, agent_buf, 2)

        # 4. Wrap as WAV.
        return _wrap_wav(mixed, sample_rate=MIX_SAMPLE_RATE, sample_width=2)

    def _render_agent_timeline(self) -> tuple[bytearray, float]:
        """Build the agent's PCM16 16k timeline. Returns (buffer, total_seconds)."""
        if not self._agent_chunks:
            return bytearray(), 0.0

        # Resample every chunk to 16 kHz once, then walk turns.
        rendered: list[tuple[float, bytes]] = []
        for arrival, pcm24k in self._agent_chunks:
            chunk_16k, _ = audioop.ratecv(
                pcm24k, 2, 1, AGENT_SAMPLE_RATE, MIX_SAMPLE_RATE, None
            )
            rendered.append((arrival, chunk_16k))

        # Single sweep: concatenate within a turn, jump to wall-clock on a gap.
        cursor_sec = rendered[0][0]
        last_arrival = rendered[0][0]
        bytes_per_sec = MIX_SAMPLE_RATE * 2

        # Pre-size to a comfortable upper bound; we'll trim at the end.
        upper_bound_sec = self.total_seconds_estimate(rendered)
        buf = bytearray(int(upper_bound_sec * bytes_per_sec) + bytes_per_sec)

        max_byte_written = 0
        for arrival, chunk_16k in rendered:
            if arrival - last_arrival > self._TURN_GAP_SECONDS:
                cursor_sec = arrival  # new turn — jump forward, leaving silence
            chunk_secs = len(chunk_16k) / bytes_per_sec
            start = int(cursor_sec * MIX_SAMPLE_RATE) * 2
            end = start + len(chunk_16k)
            if end > len(buf):
                buf.extend(b"\x00" * (end - len(buf)))
            buf[start:end] = chunk_16k
            max_byte_written = max(max_byte_written, end)
            cursor_sec += chunk_secs
            last_arrival = arrival

        return buf[:max_byte_written], max_byte_written / bytes_per_sec

    @staticmethod
    def total_seconds_estimate(rendered: list[tuple[float, bytes]]) -> float:
        if not rendered:
            return 0.0
        last_arrival, last_chunk = rendered[-1]
        bytes_per_sec = MIX_SAMPLE_RATE * 2
        return last_arrival + len(last_chunk) / bytes_per_sec + 1.0

    # ------------------------------------------------------------------
    # GCS path
    # ------------------------------------------------------------------

    def gcs_folder(self) -> str:
        """``YYYY-MM-DD/HH-MM-SS_<sid>`` (no trailing slash)."""
        date_part = self.started_at.strftime("%Y-%m-%d")
        time_part = self.started_at.strftime("%H-%M-%S")
        return f"{date_part}/{time_part}_{self.call_sid}"


def _fit_to_size(buf: bytes, target_bytes: int) -> bytes:
    """Pad with PCM16 silence (zero bytes) or truncate to ``target_bytes``."""
    if len(buf) == target_bytes:
        return buf
    if len(buf) < target_bytes:
        return buf + b"\x00" * (target_bytes - len(buf))
    return buf[:target_bytes]


def _wrap_wav(pcm: bytes, *, sample_rate: int, sample_width: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()
