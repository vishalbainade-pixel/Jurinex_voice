# Dataflow

Step-by-step trace of what happens on every call. Use this with the
console output (Rich panels + `log_dataflow` lines) and the DB tables
in [README §7](../README.md#7-database-schema) to debug end-to-end.

---

## 1. Inbound call (someone dials our Twilio number)

### 0. Prerequisite — Twilio webhook is pointing at us

In Twilio Console → Phone Numbers → `+18159348556` (or whatever number
you own):

- **A Call Comes In** → Webhook → `POST` →
  `https://<PUBLIC_BASE_URL>/twilio/incoming-call`
- **Status callback** (optional) →
  `https://<PUBLIC_BASE_URL>/twilio/call-status`

`<PUBLIC_BASE_URL>` is whatever you set in `.env`
(your ngrok URL locally, your Cloud Run URL in production). Twilio dials
whatever URL is configured on the **number itself** in the Console — that
must match what the running app expects.

### 1.1 Caller dials → Twilio fetches our TwiML

```
+91xxxxxxxxxx ──┐
                ▼
  Twilio answers → POST /twilio/incoming-call
                   form fields: CallSid, From, To, etc.
```

Handler: [`app/api/twilio_routes.py incoming_call`](../app/api/twilio_routes.py)

1. Logs a `📞 INBOUND CALL` Rich panel with From / To / Call SID.
2. Calls `CallService.record_inbound_webhook(...)` (writes one debug
   breadcrumb).
3. Builds **TwiML** via `_build_twiml_stream(...)` — XML-escaped
   properly so the `&` in the WebSocket URL doesn't break the document
   (this was the bug that caused *"an application error has occurred"*
   the first time around):

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <Response>
     <Connect>
       <Stream url="wss://<host>/twilio/media-stream?call_sid=CAxxx&amp;direction=inbound">
         <Parameter name="direction" value="inbound"/>
         <Parameter name="from"      value="+91xxxxx"/>
         <Parameter name="to"        value="+18159348556"/>
         <Parameter name="call_sid"  value="CAxxx"/>
       </Stream>
     </Connect>
     <Say voice="alice">Sorry, the assistant is currently unavailable. Please call back later.</Say>
   </Response>
   ```

   The `<Say>` only fires if the `<Connect>`/`<Stream>` bridge fails.
   In normal operation Twilio holds the call open against the WebSocket
   and `<Say>` never runs.
4. Returns the TwiML as `application/xml`. Twilio receives it.

Stages logged: `twilio.webhook.received`, `twilio.twiml.generated`.

### 1.2 Twilio opens a WebSocket → media stream begins

Twilio opens a long-lived bidirectional WebSocket to:

```
wss://<PUBLIC_BASE_URL>/twilio/media-stream
```

Handler: [`app/realtime/twilio_media_stream.py TwilioMediaStreamHandler.handle()`](../app/realtime/twilio_media_stream.py)

The protocol is JSON event frames, one of:

| Twilio event | What we do |
| --- | --- |
| `connected` | Log handshake (`twilio.media.connected`). |
| `start`     | Kickoff event with `streamSid`, `callSid`, `customParameters`. Triggers full call setup (§1.3). |
| `media`     | One 20 ms μ-law audio frame from the caller. Buffered + forwarded to Gemini (§1.5). |
| `mark`      | Echo of marks we sent. Logged. |
| `stop`      | Caller hung up or Twilio is closing. Triggers teardown (§1.8). |

Stages logged: `twilio.websocket.accepted`, `twilio.media.connected`.

### 1.3 The `start` event — wiring the whole call

Inside `_on_start()`:

```
                 ┌────────────────────────────────────────────────────┐
                 │ DB: INSERT calls (direction='inbound', status='started') │
                 │ DB: INSERT call_debug_events (twilio.media.start)        │
                 │ session_manager.create() → in-memory CallSession         │
                 │ Rich panel: 📞 CALL STARTED                              │
                 │ Hook gemini.on_session_dead → auto-hangup                │
                 │ Initialize CallRecorder (taps caller + agent audio)      │
                 │ gemini.connect(session_id, JURINEX_PREETI_PROMPT)        │
                 │ Spawn _consume_gemini_events task                        │
                 │ Spawn _watchdog_loop task                                │
                 └────────────────────────────────────────────────────┘
```

`gemini.connect` opens the Gemini Live WebSocket via
`client.aio.live.connect(model=GEMINI_MODEL, config=LiveConnectConfig(...))`.
The config includes:

- `response_modalities=["AUDIO"]`
- `speech_config.voice_config.prebuilt_voice_config.voice_name=GEMINI_VOICE` (Aoede)
- `system_instruction=JURINEX_PREETI_SYSTEM_PROMPT` (persona + tool-use rules)
- `input_audio_transcription` + `output_audio_transcription` so transcripts populate `call_messages`
- `tools=[Tool(function_declarations=[...])]` (the four schemas:
  `search_knowledge_base`, `transfer_to_human_agent`,
  `create_support_ticket`, `end_call`)

Two background tasks now run for the lifetime of the call:

- **`_consume_gemini_events`** — drains Gemini events from the receive
  queue and routes them to either the Twilio WS (audio out) or the DB
  (transcripts) or `dispatch_tool_call` (function calls).
- **`_watchdog_loop`** — once per second, checks: silence >
  `SILENCE_TIMEOUT_SECONDS`? call age > `MAX_CALL_DURATION_SECONDS`? →
  triggers `_graceful_hangup`.

Stages logged: `twilio.media.start`, `gemini.session.create`,
`gemini.session.open`, `recorder.armed`, `watchdog.armed`.

### 1.4 Preeti speaks first

Twilio's `media` frames begin arriving immediately after `start` —
that's enough to trigger Gemini Live to emit the opening turn. Within
~1 second the caller hears:

> *"Hello, thank you for contacting Jurinex support. This is Preeti.
>  I can help you in English, Hindi, or Marathi. Which language would
>  you prefer?"*

(English-only opener — the prompt forbids reciting all three languages.)

Each Gemini audio chunk that arrives at `_handle_gemini_event(event.type == "audio")`:

1. Is **tapped** for the `CallRecorder` (raw 24 kHz PCM — used later to
   build `recording.wav`).
2. Goes through the stateful `Pcm24kToMulaw8k` resampler → 8 kHz μ-law.
3. Is **chunked** into 160-byte (20 ms) frames — Twilio's expected
   frame size.
4. Is base64-encoded and sent back as a `media` event on the same
   WebSocket.

Stages logged: `gemini.response.audio` (or transcripts, see §1.6),
`twilio.media.outbound`.

### 1.5 Caller speaks → audio flows to Gemini

Every `media` event from Twilio (every 20 ms) hits `_on_media`:

1. Base64-decode → raw μ-law 8 kHz.
2. **Tap** the raw μ-law for the `CallRecorder` (caller side).
3. μ-law 8 kHz → PCM16 8 kHz → PCM16 16 kHz (stdlib `audioop`).
4. Compute RMS — if above the speech threshold, refresh
   `_last_mic_activity_ts` (this is what the silence watchdog watches;
   constant-silence frames otherwise would never trigger a timeout).
5. Append to a 100 ms mic buffer. Once it hits 3200 bytes, **flush** to
   Gemini via
   `session.send_realtime_input(audio=Blob(data=..., mime_type="audio/pcm;rate=16000"))`.

Why 100 ms batches instead of 20 ms? Sending 50 frames/second was
causing Gemini's WS to ping-timeout. ~10 batches/second is stable.

Stages logged: `twilio.media.flush`, `gemini.audio.input`.

### 1.6 Conversation with tools

`_extract_response` translates Gemini events into our `GeminiEvent`
shape and routes them:

| event.type | What happens |
| --- | --- |
| `audio` (PCM16/24k) | Played back via the Twilio out path (§1.4) and tapped for recording |
| `input_transcript`  | Caller's speech transcript → `call_messages` as `Speaker.customer` |
| `output_transcript` | Preeti's spoken reply transcript → `call_messages` as `Speaker.agent` |
| `tool_call`         | Routed to `dispatch_tool_call` → one of the four tools |
| `error` / `session_close` | Logged; if the session dies, `_disable_session` fires `on_session_dead` → `_graceful_hangup(reason='gemini_failure')` (when `AUTO_HANGUP_ON_GEMINI_FAILURE=true`) |

Stages logged: `gemini.transcript.input`, `gemini.transcript.output`,
`gemini.tool_call`, `gemini.receive_loop_error`.

#### Tool-call subflow (e.g. KB search)

1. Gemini emits `tool_call(name='search_knowledge_base', args={'query': ...})`.
2. `_handle_tool_call` → `dispatch_tool_call` → `kb_tools.search_knowledge_base`.
3. The service embeds the query (`gemini-embedding-001`, 768-d,
   `RETRIEVAL_QUERY`), runs the cosine SQL on `kb_chunks`, writes a
   `kb_search_logs` row.
4. The result (top chunks + `confident` flag) is sent back to Gemini
   via `gemini.send_text(...)` so the model can phrase its spoken reply.
5. An `agent_tool_events` audit row is written. A `🗄️ KB SEARCH` Rich
   panel fires.

If the top score < `KB_MIN_SCORE` (default 0.60), Preeti is instructed
to call `transfer_to_human_agent` instead → that tool replaces the live
TwiML with
`<Response><Say>Connecting you...</Say><Dial callerId="+1815...">+91 78858 20020</Dial></Response>`,
and Twilio bridges the caller to the admin number. Recording continues
across the bridge.

Stages logged: `tool.dispatch`, `tool.kb.search`, `kb.search.done`,
`tool.transfer_to_human`, `twilio.hangup.twiml`.

### 1.7 Watchdogs running silently

The `_watchdog_loop` task ticks once per second. If either threshold is
exceeded:

- **Silence** (`SILENCE_TIMEOUT_SECONDS=30`): asks Preeti to politely
  say goodbye in the active language, waits `FAREWELL_GRACE_SECONDS=3`,
  then drops the line via `CallService.hangup_twilio_call`.
- **Max duration** (`MAX_CALL_DURATION_SECONDS=600`): same flow but the
  goodbye says "we've reached the time limit, please call back".

Either fires a `⚠️ AUTO HANGUP` Rich panel.

Stages logged: `watchdog.armed`, `watchdog.silence_timeout`,
`watchdog.max_duration`, `watchdog.gemini_dead`,
`watchdog.farewell_error`.

### 1.8 Caller hangs up → `stop` → teardown

When the caller hangs up, Twilio sends a `stop` event and closes the
WebSocket. `handle()`'s `finally` runs `_teardown()`:

1. **Flush** any leftover buffered mic audio to Gemini so the last
   utterance isn't truncated.
2. **Close** the Gemini session.
3. **Cancel** the consumer task and the watchdog task.
4. **Build the recording**: the `CallRecorder` produces one
   timeline-mixed `recording.wav` (caller continuous from t=0; agent
   chunks placed at their arrival times with gaps preserved; mixed to
   PCM16 16 kHz mono).
5. **Upload** to GCS at
   `gs://<GCS_BUCKET>/YYYY-MM-DD/HH-MM-SS_<call_sid>/` —
   `recording.wav` + `metadata.json`. Stores the URIs back into
   `calls.raw_metadata`.
6. **Mark the call completed**: `status=completed`, `ended_at=now()`,
   `duration_seconds=...`, builds and stores `summary` from
   `call_messages`.
7. **Logs** a `🏁 CALL ENDED` Rich panel.
8. **Removes** the in-memory `CallSession` from `session_manager`.
9. **Closes** the WebSocket.

Throughout the call, status-callback hits to `POST /twilio/call-status`
are logged (and persisted as debug events) so the console shows the
lifecycle: `initiated → ringing → in-progress → completed`.

Stages logged: `twilio.media.stop`, `gemini.session.close`,
`recorder.flush`, `gcs.uploaded`, `call.summary.created`,
`twilio.call.status`.

### 1.9 End-state — what's in the DB / GCS after a single inbound call

| Where | What |
| --- | --- |
| `customers`         | One row per unique phone number that has called |
| `calls`             | One row for *this* call (sid, direction, status, language, summary, duration, GCS URIs in `raw_metadata`) |
| `call_messages`     | One row per turn: caller transcripts (`speaker=customer`) and Preeti transcripts (`speaker=agent`) |
| `agent_tool_events` | One row per tool the model invoked, with input/output JSON |
| `support_tickets`   | One row per ticket Preeti opened (if any) |
| `escalations`       | One row if she transferred to a human |
| `kb_search_logs`    | One row per `search_knowledge_base` call with top chunk IDs + scores |
| `call_debug_events` | Persisted dataflow stages (`twilio.media.*`, `gemini.session.*`, `watchdog.*`, `tool.*`, `gcs.*`) |
| **GCS**             | `gs://<GCS_BUCKET>/YYYY-MM-DD/HH-MM-SS_<sid>/recording.wav` + `metadata.json` |

### 1.10 Quick mental model

```
caller picks up
   │
   ▼
Twilio POST /twilio/incoming-call ─► we return TwiML <Connect><Stream/></Connect>
   │
   ▼
Twilio opens WS /twilio/media-stream
   │
   ├─ event=start  ──► DB row, recorder, watchdog, Gemini session
   │
   ├─ event=media (every 20ms) ──► resample → batch 100ms → Gemini Live
   │                              (also tap for recording)
   │
   ├─ events from Gemini:
   │      audio        → resample → 20ms μ-law → Twilio WS (and tap for recording)
   │      transcripts  → call_messages
   │      tool_call    → dispatch_tool_call
   │                       ├─ search_knowledge_base   (RAG over kb_chunks)
   │                       ├─ transfer_to_human_agent (Twilio <Dial> bridge)
   │                       ├─ create_support_ticket
   │                       ├─ escalate_to_human
   │                       └─ end_call
   │
   ├─ watchdog:
   │      silence > SILENCE_TIMEOUT_SECONDS → graceful hangup
   │      duration > MAX_CALL_DURATION_SECONDS → graceful hangup
   │      Gemini died → graceful hangup (if AUTO_HANGUP_ON_GEMINI_FAILURE)
   │
   └─ event=stop  ──► flush, close Gemini, build recording, upload GCS,
                       finalize calls row, write summary, close WS
```

---

## 2. Outbound call (admin triggers an outbound dial)

```
POST /admin/outbound-call (X-Admin-API-Key)
        │
        ▼
CallService.place_outbound
        │
        ├─ normalize_e164
        ├─ Twilio REST: client.calls.create(url=/twilio/outbound-answer)
        └─ persist Call row (direction=outbound, status=started)
        │
        ▼
Customer answers → Twilio POSTs /twilio/outbound-answer
        │
        ▼
Same TwiML <Connect><Stream …/> path as inbound; from here on the flow
is identical to §1.2 onward (start event → Gemini session → watchdogs →
audio bridge → tools → teardown → GCS recording).
```

Twilio status callbacks (`initiated → ringing → in-progress → completed`)
hit `POST /twilio/call-status` throughout. On trial accounts the
destination number must be on **Verified Caller IDs**.

---

## 3. Stage names emitted via `log_dataflow`

These are the strings to grep for in the console or the
`call_debug_events` table. Anything in `_PERSIST_PREFIXES` (in
[`app/observability/logger.py`](../app/observability/logger.py)) is
also written to the DB on a fire-and-forget background task.

```
# Twilio webhooks / WebSocket
twilio.webhook.received
twilio.twiml.generated
twilio.websocket.accepted
twilio.websocket.disconnected
twilio.media.connected
twilio.media.start
twilio.media.chunk
twilio.media.flush
twilio.media.outbound
twilio.media.outbound_error
twilio.media.stop
twilio.media.invalid_json
twilio.media.unknown
twilio.media.mark
twilio.call.status
twilio.hangup.completed
twilio.hangup.twiml
twilio.hangup.skipped

# Gemini Live
gemini.session.create
gemini.session.open
gemini.session.close
gemini.transcription
gemini.tools.declare_failed
gemini.audio.input
gemini.audio.input_error
gemini.text.input
gemini.text.input_error
gemini.response.text
gemini.transcript.input
gemini.transcript.output
gemini.tool_call
gemini.turn.complete
gemini.receive_loop_error
gemini.receive_loop_ended
gemini.on_session_dead.error

# Knowledge base (RAG)
kb.agent_id.resolved
kb.agent_id.lookup_error
kb.embed.empty
kb.embed.dim_mismatch
kb.search.done
kb.search.log_error

# Database
db.message.saved
db.ping

# Tools
tool.dispatch
tool.lookup_customer
tool.kb.search
tool.ticket.create
tool.escalation.create
tool.case.status
tool.transfer_to_human
tool.end_call

# Watchdogs (auto-hangup safety net)
watchdog.armed
watchdog.silence_timeout
watchdog.max_duration
watchdog.gemini_dead
watchdog.cancelled
watchdog.farewell_error

# Recording / GCS
recorder.armed
recorder.flush
gcs.auth
gcs.uploaded
gcs.skipped

# Call lifecycle
call.summary.created
```
