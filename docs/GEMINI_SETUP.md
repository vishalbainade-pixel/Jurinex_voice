# Gemini Setup

## 1. API key

Set either `GEMINI_API_KEY` or `GOOGLE_API_KEY` in `.env`. If both are present
`GEMINI_API_KEY` wins.

```env
GEMINI_API_KEY=AQ.Ab8RN6...
GEMINI_MODEL=gemini-3.1-flash-live-preview
```

## 2. SDK

We use `google-genai`. The `GeminiLiveClient` wraps the live session so the
rest of the app speaks our `GeminiEvent` dataclass regardless of SDK changes.

## 3. Real-time audio bridging — TODOs

Twilio sends μ-law 8kHz audio; Gemini Live currently expects PCM16 16kHz. The
exact bridge (resample + companding + back-pressure) is the largest realtime
work item. See:

- `app/realtime/audio_codec.py` — codec helpers
- `app/realtime/gemini_live_client.py` — `send_audio` / `receive_events`
- `app/realtime/twilio_media_stream.py` — `_on_media` and `_consume_gemini_events`

Until that bridge is wired:

- `DEMO_MODE=true` makes `GeminiLiveClient` return deterministic text replies.
- This lets the rest of the system (DB, tools, transcripts, lifecycle) be
  exercised end-to-end without depending on the live audio plumbing.

## 4. Switching to real Gemini

1. Pin a known-good `google-genai` version.
2. In `GeminiLiveClient.connect`, open the live session and store it on
   `self._real_session`.
3. In `send_audio`, push PCM16 16kHz frames into the live session input.
4. In `receive_events`, translate SDK events into `GeminiEvent`s and push to
   `self._inbox`.
5. Set `DEMO_MODE=false`.
