# Jurinex_call_agent — Preeti, the multilingual support voice agent

Production-grade Python backend for a multilingual (English / Hindi / Marathi)
AI voice customer-support agent named **Preeti**, built for the **Jurinex**
legal-AI platform. The bridge connects a Twilio phone number to Google's
**Gemini Live API**, hot-loads its persona / model / voice / tools / knowledge
base from a shared Postgres schema that the **admin dashboard** writes into,
runs RAG against admin-uploaded documents, books real Google Calendar
demos, hands the line off to humans on consent, and writes a full audit
trail back to the same admin schema.

The result is a single Python service that is fully **DB-driven**: the
admin team can edit prompts, swap voices, change models, add tools,
reroute transfers, schedule outbound calls, and adjust working hours —
all from their dashboard, with effects landing on the next call (or
within a 60-second cache TTL) and **without redeploying this service**.

---

## Table of contents

1. [What this project does](#1-what-this-project-does)
2. [High-level architecture](#2-high-level-architecture)
3. [Quickstart](#3-quickstart)
4. [Configuration model — DB-driven vs env-driven](#4-configuration-model)
5. [Database schema reference](#5-database-schema-reference)
6. [Voice agent runtime — call lifecycle](#6-voice-agent-runtime)
7. [Tools the model can call](#7-tools-the-model-can-call)
8. [Knowledge base (RAG)](#8-knowledge-base-rag)
9. [Calendar booking (`calendar_check` / `calendar_book`)](#9-calendar-booking)
10. [Live human transfer (`transfer_call`)](#10-live-human-transfer)
11. [Agent hot-swap (`agent_transfer`)](#11-agent-hot-swap)
12. [Outbound call scheduler](#12-outbound-call-scheduler)
13. [Post-call extraction + enrichment](#13-post-call-extraction--enrichment)
14. [Pricing per call](#14-pricing-per-call)
15. [Voice catalogue validation](#15-voice-catalogue-validation)
16. [Observability — Rich console + dataflow logs](#16-observability)
17. [HTTP API surface](#17-http-api-surface)
18. [Operational runbook](#18-operational-runbook)
19. [Testing & smoke checks](#19-testing--smoke-checks)
20. [File layout](#20-file-layout)
21. [Phase history](#21-phase-history)

---

## 1. What this project does

```
INBOUND
  Caller → Twilio number → /twilio/incoming-call (TwiML) →
    /twilio/media-stream (WebSocket) → GeminiLiveClient → Preeti speaks →
    Tools (KB / transfer / calendar / agent_transfer / end_call) →
    Recording → Post-call extraction → Cost stamp → Audit rows in PG

OUTBOUND
  Admin schedules a row in voice_call_schedules (form / CSV / API) →
    Background poller claims it (FOR UPDATE SKIP LOCKED) →
    CallService dials Twilio → /twilio/outbound-answer →
    same media-stream / Gemini / tools / extraction pipeline →
    schedule row marked completed with call_id linked
```

Preeti is grounded in your Jurinex product documentation via pgvector
cosine search against the admin-owned `kb_chunks` table. When confident
she answers from the chunks; when not, she asks the caller for consent
to transfer and bridges the line to a human via Twilio `<Dial>`.

`DEMO_MODE=true` short-circuits Gemini with a deterministic simulator
for end-to-end testing without telephony or a live API key.

---

## 2. High-level architecture

```
                          ┌─────────────────────────────────────┐
                          │        ADMIN DASHBOARD              │
                          │                                     │
                          │   • Voice agent builder             │
                          │   • Prompt fragments + tool prompts │
                          │   • Transfer routing rules          │
                          │   • KB document upload              │
                          │   • Outbound call scheduler         │
                          └────────────────────┬────────────────┘
                                               │ writes
                                               ▼
                  ┌────────────────────────────────────────────────────┐
                  │  Cloud SQL PostgreSQL  (the shared admin schema)   │
                  │                                                    │
                  │   voice_agents                                     │
                  │   voice_agent_configurations                       │
                  │   voice_agent_transfer_configs                     │
                  │   voice_system_prompt_fragments                    │
                  │   voice_tool_system_prompts                        │
                  │   kb_documents / kb_chunks (pgvector)              │
                  │   platform_voices                                  │
                  │   voice_model_pricing                              │
                  │   voice_call_schedules                             │
                  │   voice_tool_executions                            │
                  │   voice_calendar_bookings                          │
                  │   voice_post_call_extractions                      │
                  │   voice_call_enrichments                           │
                  │   voice_debug_events                               │
                  │                                                    │
                  │   + the bridge's own tables:                       │
                  │   calls, call_messages, support_tickets,           │
                  │   escalations, call_debug_events, …                │
                  └────────────────────────────────────────────────────┘
                                               ▲
                                               │ reads + writes
       ┌───────────────────────────────────────┴──────────────────────────────┐
       │           THIS SERVICE (Jurinex_call_agent / FastAPI)                │
       │                                                                      │
       │   ┌────────────────┐   ┌────────────────┐   ┌────────────────────┐   │
       │   │ api/           │   │ services/      │   │ realtime/          │   │
       │   │  twilio_routes │──▶│  call_service  │──▶│  twilio_media_…    │◄──│── Twilio Media Streams (WS)
       │   │  admin_routes  │   │  scheduler     │   │  gemini_live_…     │◄──│── Gemini Live API (WS)
       │   │  health/debug  │   │  post_call     │   │  call_recorder     │   │
       │   └────────────────┘   │  pricing       │   └────────────────────┘   │
       │                        │  google_cal…   │                            │
       │                        │  kb_search     │                            │
       │   ┌────────────────┐   └────────────────┘   ┌────────────────────┐   │
       │   │ tools/         │                        │ db/                │   │
       │   │  kb_tools      │                        │  voice_agent_repo  │   │
       │   │  transfer      │                        │  prompt_fragments… │   │
       │   │  calendar      │                        │  voice_call_sched… │   │
       │   │  agent_transfer│                        │  voice_calendar…   │   │
       │   │  end_call /    │                        │  voice_tool_exec…  │   │
       │   │  ticket / …    │                        │  voice_post_call…  │   │
       │   └────────────────┘                        │  voice_debug_evt…  │   │
       │                                             │  platform_voices   │   │
       │                                             └────────────────────┘   │
       │                                                                      │
       │   GCS Storage (mixed-mono WAV recordings, per-call folder)           │
       └──────────────────────────────────────────────────────────────────────┘
```

See also: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/DATAFLOW.md`](docs/DATAFLOW.md), [`docs/SCHEDULER.md`](docs/SCHEDULER.md),
[`docs/PHASE3.md`](docs/PHASE3.md).

---

## 3. Quickstart

### 3.1 Clone + venv

```bash
git clone <repo> jurinex
cd Jurinex_call_agent
cp .env.example .env       # then edit values

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3.2 Minimum `.env` to boot

```dotenv
APP_ENV=development
DEMO_MODE=false
PUBLIC_BASE_URL=https://your-ngrok-or-public-host
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+1...
GOOGLE_API_KEY=AIza...
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db
SYNC_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/db
ADMIN_API_KEY=change_me
KB_AGENT_NAME=preeti           # which voice_agents.name this bridge serves by default
JURINEX_VOICE_DEFAULT_CALENDAR_ID=...@group.calendar.google.com
JURINEX_VOICE_CALENDAR_SA_JSON_BASE64=...    # base64 of SA JSON
SCHEDULER_ENABLED=false        # flip to true to start the outbound poller at boot
```

The full list with operator notes is in [`.env.example`](.env.example).
Lines that are commented out in the production `.env` represent
"DB-driven, the env value is the fallback for degraded mode" — see
[§4](#4-configuration-model).

### 3.3 Run locally

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Then expose `:8000` to the public internet (e.g. with ngrok) and point
the Twilio number's voice webhook at `<PUBLIC_BASE_URL>/twilio/incoming-call`.

### 3.4 Run with Docker Compose

```bash
docker compose up -d --build
docker compose logs -f app
```

The container runs `alembic upgrade head` before launching uvicorn.

### 3.5 Smoke an inbound call

Once the bridge is up and the Twilio number's webhook points at it,
call the number from your phone. You should see (in order):

```
🔌 INCOMING CALL                       From=… Call SID=…
INFO  agent.routing.selected           requested='preeti' source=env
INFO  agent.bundle.loaded              live_model=… voice=…
INFO  prompt.assembled                 sections=N tools=[…]
🤖  GEMINI SESSION STARTED              model=… voice=…
📞  CALL STARTED                        Direction=inbound …
… (your conversation) …
🏁  CALL ENDED                          recording=gs://…
INFO  post_call.done                   keys=[call_summary,…] successful=True
```

If you don't see the `agent.bundle.loaded` line, the bridge is in
"static fallback" mode — see [§4](#4-configuration-model).

---

## 4. Configuration model

The bridge has two layers of configuration:

| Layer | Source | Wins over |
|---|---|---|
| **DB bundle** | `voice_agents` + `voice_agent_configurations` + `voice_agent_transfer_configs` | env vars, static prompt files |
| **Env vars** (`.env`) | infra / secrets / fallbacks | – |

On every call `_on_start` does this in order:

1. Pick the agent name (Twilio `customParameters.agent_name` if present,
   else `KB_AGENT_NAME`).
2. `VoiceAgentRepository.load_active_bundle(name)` — refuses to dial if
   `voice_agents.status != 'active'`.
3. If the bundle is missing/inactive: log `prompt.source: static fallback`
   at WARNING and use the static `JURINEX_PREETI_SYSTEM_PROMPT` + env model/voice.
4. If the bundle is present:
   - `live_model`, `voice_name`, `temperature` come from `voice_agent_configurations`.
   - System instruction is assembled from `voice_system_prompt_fragments`
     + `voice_tool_system_prompts` + the persona stored in
     `voice_agent_configurations.audio_live_system_prompt`.
   - Enabled tools come from `agent_builder.functions[]` + auto-include
     `search_knowledge_base` when `agent_builder.knowledge_base.document_ids`
     is non-empty.
   - `call.max_duration_minutes` and `call.end_on_silence_minutes` from
     the bundle override the watchdog defaults.
   - Transfer destination(s) come from `voice_agent_transfer_configs`
     (static or dynamic — see [§10](#10-live-human-transfer)).

### 4.1 What's in `.env` (and why each line is there)

| Group | Keys | Notes |
|---|---|---|
| App | `APP_NAME`, `APP_ENV`, `DEBUG`, `LOG_LEVEL`, `DEMO_MODE`, `PUBLIC_BASE_URL` | Boot / public URL Twilio webhooks hit |
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` | REST creds + caller-id for outbound |
| Gemini | `GOOGLE_API_KEY` | Picked up by google-genai. `GEMINI_MODEL` / `GEMINI_VOICE` are commented — DB bundle wins |
| DB | `DATABASE_URL`, `SYNC_DATABASE_URL`, `JURINEX_VOICE_DATABASE_URL` | Async runtime + Alembic + admin DB |
| Auth | `SECRET_KEY`, `ADMIN_API_KEY` | Local API gating |
| KB | `KB_ENABLED`, `KB_AGENT_NAME`, `KB_EMBEDDING_MODEL`, `KB_EMBEDDING_DIM`, `KB_SEARCH_K`, `KB_MIN_SCORE`, `KB_SHADOW_*`, `KB_CHUNK_TOKENS`, `KB_CHUNK_OVERLAP` | RAG retrieval knobs |
| Greeting | `EAGER_GREETING_ENABLED`, `EAGER_GREETING_AUDIO_URL` | Pre-rendered WAV played the moment the call connects |
| Transfer (commented) | `SUPPORT_ADMIN_PHONE`, `TRANSFER_DIAL_TIMEOUT_SECONDS` | DB bundle wins; only used when no transfer config row exists for the agent |
| Transfer voice | `TRANSFER_HOLD_VOICE_EN/HI/MR` | Twilio `<Say voice=…>` for the on-hold message — not in admin DB |
| Watchdog | `AUTO_HANGUP_ON_GEMINI_FAILURE`, `FAREWELL_GRACE_SECONDS`, `TECHNICAL_FAILURE_MESSAGE` | Authoritative here — not modeled in admin DB |
| Watchdog (commented) | `SILENCE_TIMEOUT_SECONDS`, `MAX_CALL_DURATION_SECONDS` | DB bundle wins (`agent_builder.call.*_minutes`) |
| GCS recordings | `GCS_RECORDINGS_ENABLED`, `GCS_BUCKET`, `GCS_PROJECT_ID`, `GCS_KEY_BASE64` | Recording uploads |
| Live caches | `JURINEX_VOICE_TOOL_PROMPT_CACHE_MS`, `JURINEX_VOICE_PROMPT_FRAGMENT_CACHE_MS` | TTL caches in front of the prompt-fragment / tool-prompt tables. Default 60s |
| Live session | `JURINEX_VOICE_LIVE_WELCOME_TIMEOUT_MS`, `JURINEX_VOICE_LIVE_TOOL_END_GRACE_MS`, `JURINEX_VOICE_LIVE_MAX_RESUMES`, `JURINEX_VOICE_LIVE_KB_BUDGET_BYTES`, `JURINEX_VOICE_LIVE_KB_CHUNKS_PER_DOC` | Resilience + KB injection caps |
| Calendar | `JURINEX_VOICE_DEFAULT_CALENDAR_ID`, `JURINEX_VOICE_DEFAULT_CALENDAR_TZ`, `JURINEX_VOICE_CALENDAR_SA_JSON_BASE64`, `JURINEX_VOICE_CALENDAR_ALLOW_ATTENDEES` | Falls back when bundle's `calendar_id` is empty. DWD off → no invite emails |
| Scheduler | `SCHEDULER_ENABLED`, `SCHEDULER_POLL_SECONDS`, `SCHEDULER_MAX_INFLIGHT`, `SCHEDULER_DEFAULT_COUNTRY_CODE` | Outbound poller |

---

## 5. Database schema reference

### 5.1 Admin-owned tables (read by this service; written by the admin app)

| Table | Used for | Repo / file |
|---|---|---|
| `voice_agents` | Identity + status of each agent | [`voice_agent_repository.py`](app/db/voice_agent_repository.py) |
| `voice_agent_configurations` | Live model, voice, persona prompt, tool_settings, custom_settings (the `agent_builder` blob) | same |
| `voice_agent_transfer_configs` | Static / dynamic transfer routing | same |
| `voice_system_prompt_fragments` | Reusable prompt blocks (`live_session_base`, `live_session_realtime_rules`, `knowledge_base_header`, `welcome_turn_template`, `fallback_phrase`, …) | [`prompt_fragments_repository.py`](app/db/prompt_fragments_repository.py) |
| `voice_tool_system_prompts` | Per-tool system blocks (`search_knowledge_base`, `transfer_call`, `calendar_check`, `calendar_book`, `agent_transfer`, `end_call`) | same |
| `kb_documents` / `kb_chunks` | RAG corpus (pgvector cosine) | [`kb_search.py`](app/services/kb_search.py) |
| `kb_search_logs` | Per-search audit | same |
| `platform_voices` | Catalogue of valid Gemini voices | [`platform_voices_repository.py`](app/db/platform_voices_repository.py) |
| `voice_model_pricing` | Per-model USD/min + INR/min for cost stamping | [`pricing_service.py`](app/services/pricing_service.py) |
| `voice_call_schedules` | Outbound schedule queue | [`voice_call_schedules_repository.py`](app/db/voice_call_schedules_repository.py) |

### 5.2 Tables this service writes to

| Table | When | Writer |
|---|---|---|
| `voice_tool_executions` | Every tool dispatch (pending → completed/failed, latency_ms) | [`voice_tool_executions_repository.py`](app/db/voice_tool_executions_repository.py) |
| `voice_calendar_bookings` | Every `calendar_book` call (booked / failed) | [`voice_calendar_bookings_repository.py`](app/db/voice_calendar_bookings_repository.py) |
| `voice_post_call_extractions` | After every call: structured insights + transcript | [`voice_post_call_repository.py`](app/db/voice_post_call_repository.py) |
| `voice_call_enrichments` | Upserted per call_id: rolled-up summary + cost + recording URI | same |
| `voice_debug_events` | Bridge stage events (call.started, agent.swap, auto_hangup, call.ended) | [`voice_debug_events_repository.py`](app/db/voice_debug_events_repository.py) |
| `calls`, `call_messages`, `support_tickets`, `escalations`, `agent_tool_events`, `call_debug_events` | This service's own audit | [`repositories.py`](app/db/repositories.py) |

---

## 6. Voice agent runtime

### 6.1 Inbound call sequence

```
Twilio → POST /twilio/incoming-call
     → returns TwiML <Connect><Stream wss://…/twilio/media-stream>
     → Twilio opens WebSocket
     → TwilioMediaStreamHandler.handle()
        ├─ event=connected   → handshake
        ├─ event=start       → _on_start():
        │     1. Pick agent_name (customParameters or env)
        │     2. Load AgentBundle from DB (status='active' gate)
        │     3. Build SystemInstruction via SystemInstructionBuilder
        │     4. Validate voice_name against platform_voices catalogue
        │     5. Open GeminiLiveClient with bundle's live_model/voice/temp/tools
        │     6. Stream pre-rendered greeting WAV to caller in parallel
        │     7. Insert calls row + call_debug_events
        │     8. Emit voice_debug_events (bridge / call.started)
        │     9. Arm watchdog (silence + max-duration from bundle.call_settings)
        ├─ event=media       → forward μ-law/8k → PCM16/16k → Gemini
        └─ event=stop        → break out → _teardown():
              1. Flush mic buffer to Gemini
              2. Close Gemini session, cancel consume task
              3. Upload mixed WAV to GCS
              4. mark_completed on calls row
              5. Build summary
              6. Stash recording URIs + pricing on calls.raw_metadata
              7. (scheduler) mark_completed on voice_call_schedules row
              8. run_post_call_extraction (gemini-2.5-flash + JSON)
              9. Emit voice_debug_events (bridge / call.ended)
```

### 6.2 Outbound call sequence

```
SchedulerService._poll_loop ticks every SCHEDULER_POLL_SECONDS
  ↓
claim_due_row()  (FOR UPDATE SKIP LOCKED) → status='queued'
  ↓
CallService.place_outbound_for_schedule(schedule_id, agent_name, to_phone)
  → twilio.calls.create(to=…, url=…/twilio/outbound-answer?schedule_id=…&agent_name=…)
  → SID=CAxxxx
  ↓
mark_dialing(schedule_id, twilio_call_sid) gated on status='queued'
  ↓
Twilio → POST /twilio/outbound-answer?schedule_id=…&agent_name=…
  → forwards both into TwiML Stream <Parameter>
  → opens WebSocket → TwilioMediaStreamHandler runs the same flow as inbound
```

### 6.3 Audio pipeline

| Direction | Format | Resampler |
|---|---|---|
| Twilio → bridge | μ-law @ 8 kHz, 20 ms frames | `audioop.ulaw2lin` + `audioop.ratecv` → PCM16 @ 16 kHz |
| Bridge → Gemini | PCM16 @ 16 kHz, batched ~100 ms | direct `send_realtime_input(audio=Blob(...))` |
| Gemini → bridge | PCM16 @ 24 kHz | `Pcm24kToMulaw8k` resampler (stateful) |
| Bridge → Twilio | μ-law @ 8 kHz in 20 ms frames | `chunk_mulaw_for_twilio` |

VAD config (constants in [`gemini_live_client.py`](app/realtime/gemini_live_client.py)):
- `start_of_speech_sensitivity = START_SENSITIVITY_LOW` (telephony noise)
- `prefix_padding_ms = 200`
- `silence_duration_ms = 500`

### 6.4 Live session resilience

When Gemini's WebSocket dies (1011 keepalive, 1008 policy, transient
network), `_disable_session(reason)` schedules `_attempt_resume(reason)`
up to `JURINEX_VOICE_LIVE_MAX_RESUMES` (default 3) times before flipping
the call to dead. Each resume re-opens the WS with the same
`system_prompt`, `live_model`, and `voice`. Console emits:

```
⚠️ GEMINI RESUME    attempt=1 of 3   trigger=…
INFO  gemini.resume.ok
…
INFO  gemini.resume.exhausted   resumes=3 limit=3 — giving up
❌ GEMINI SESSION DEAD          reason=…
```

### 6.5 Multi-agent routing

The bridge serves multiple admin-configured agents from one Twilio
number. Set `agent_name` in the TwiML `<Parameter>` per call (e.g. via
Twilio Studio per inbound number, or via the scheduler's outbound
answer URL):

```xml
<Connect>
  <Stream url="wss://…/twilio/media-stream">
    <Parameter name="agent_name" value="rohit_sales"/>
  </Stream>
</Connect>
```

Falls back to `KB_AGENT_NAME` from `.env` when the parameter is blank.

---

## 7. Tools the model can call

Declared in [`gemini_live_client.py`](app/realtime/gemini_live_client.py),
dispatched by [`tool_dispatcher.py`](app/services/tool_dispatcher.py).
Every dispatch writes a `voice_tool_executions` row (pending → completed/failed).

| Tool | Purpose | Pre-flight | Post-call write |
|---|---|---|---|
| `search_knowledge_base` | Cosine search over `kb_chunks` | bundle.knowledge_base.document_ids must include the doc | `kb_search_logs` |
| `transfer_call` (alias `transfer_to_human_agent`) | Twilio `<Dial>` bridge to a human number | destination_phone validation against `destination_prompt` | `escalations`, `agent_tool_events` |
| `calendar_check` | Read availability from Google Calendar | strict-TZ ISO timestamps | – |
| `calendar_book` | Create event with rich description | view_only / day_disabled / date_blocked / outside_working_hours / conflict | `voice_calendar_bookings` (booked / failed) |
| `agent_transfer` | Hot-swap to a different voice agent | target.id != current.id; target.status='active' | – |
| `create_support_ticket` | Open a ticket | – | `support_tickets`, `agent_tool_events` |
| `end_call` | Graceful hangup after farewell | – | `calls.status` |

When the admin enables a tool key via `agent_builder.functions[]`, the
canonical declaration name (e.g. `transfer_call`) is published to the
model — the dispatcher canonicalizes back to the legacy handler name.

### 7.1 voice_tool_executions audit row

Every tool call lands in this row:

| Column | Meaning |
|---|---|
| `id` | row UUID |
| `agent_id` | from the bundle |
| `session_id` | Twilio media stream session UUID |
| `tool_name` | the name the model used |
| `input_json` | the model's args |
| `output_json` | the handler's return value |
| `status` | `pending` → `completed` / `failed` |
| `latency_ms` | end-to-end wallclock |
| `function_call_id` | Gemini's id for the tool call |

---

## 8. Knowledge base (RAG)

### 8.1 Pgvector cosine search

`KbSearchService.search(query, k, call_id)` ([`kb_search.py`](app/services/kb_search.py)):

1. Embed `query` with `gemini-embedding-001` (dim 768, matryoshka).
2. SQL: `SELECT chunk_id, document_id, text, heading_path, 1 - (embedding <=> :q) AS score
   FROM kb_chunks WHERE document_id = ANY(...) ORDER BY embedding <=> :q ASC LIMIT :k`.
3. Compare the top score against `KB_MIN_SCORE` → `confident: bool`.
4. Insert a `kb_search_logs` row.
5. Return `{confident, top_score, results: [{document_title, heading_path, text, score}, …]}`.

### 8.2 Two paths for grounding

| Path | When | Strength |
|---|---|---|
| **Tool-driven** (model calls `search_knowledge_base`) | Default for English-speaking callers — model knows when it doesn't know | Cleanest: only fires on real product questions |
| **Shadow-RAG** (`KB_SHADOW_ENABLED=true`) | We run KB search on every caller utterance and prime Gemini's context with the top chunks | Helpful when caller speaks Hindi/Marathi (cross-language cosine is weaker, model fires the tool less) |

Shadow-RAG uses a lower threshold (`KB_SHADOW_MIN_SCORE`) and is gated to
not fire while the model is mid-turn (`_last_output_transcript_at`
within 1.5 s) — this prevented a "speaks 3 words then restarts from
word 1" loop during long transfer pitches.

---

## 9. Calendar booking

[`calendar_tools.py`](app/tools/calendar_tools.py) +
[`google_calendar.py`](app/services/google_calendar.py).

Both tools pull config from `agent_builder.tool_settings.calendar`:
`timezone`, `calendar_id`, `view_only`, `default_meeting_minutes`,
`blocked_dates`, `working_hours`. Falls back to env defaults
(`JURINEX_VOICE_DEFAULT_CALENDAR_ID`, `_TZ`) when unset.

### 9.1 `calendar_check`

- Parses `start_iso` / `end_iso` (assume UTC if naive — read-only is
  more forgiving than write).
- Pulls events via `events.list`.
- For each day in the window, intersects the working-hours block with
  busy ranges, drops slivers shorter than `default_meeting_minutes`.
- Returns `{free_windows: [{start_iso, end_iso, duration_minutes}, …]}`.

### 9.2 `calendar_book` — three layers of reliability

The thing the user typically reports as "the demo wasn't booked" is
caught by ONE of these layers:

1. **Strict TZ-aware parsing** — naive `2026-05-04T10:00:00` is rejected
   with `status: bad_timestamp` so the model retries with `+05:30`,
   instead of silently shifting the slot 5h30m and tripping
   `outside_working_hours`.
2. **Retry on transient errors** — `insert_event` is retried up to 3
   times with exponential backoff (0.4s → 0.8s → 1.6s) for 429 / 5xx /
   network errors. Hard 4xx errors fail fast.
3. **Post-insert verification** — after Google returns 200, we re-list
   the slot and confirm the event ID is present. If not, the result
   carries `verified_on_calendar: false` and the panel switches from
   green `BOOKING SUCCEEDED` to yellow `BOOKING UNCONFIRMED`. The model
   is told to confirm aloud only when both `status='booked'` AND
   `verified_on_calendar=true`.

### 9.3 Rich event description

Every event body now contains:

```
Booked via Preeti voice agent (Jurinex).

── Contact ──
Name:           Test User
Email:          test.user@example.com
Contact phone:  +918888888888
Caller phone:   +919876543210            ← auto-pulled from calls.customer_phone
Tap to call:    tel:+918888888888

── Reason ──
<description or summary>

── Reference ──
Originating call_id: 2bd160a9-…
Agent: preeti (f7055938-…)
```

Every booking (success or failure) also writes a `voice_calendar_bookings`
row whose contents are printed as a Rich table in the console:

```
🛠️ INSERT (status=booked) → voice_calendar_bookings
   ┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
   ┃ id                 ┃ 4b42a989-…                       ┃
   ┃ google_event_id    ┃ 4rk2c4svjpkprtldtm4o1esde8       ┃
   ┃ start_time / end   ┃ 2026-05-11T15:30:00+05:30 / …    ┃
   ┃ attendee_*         ┃ Console Test / …@…/ +91…         ┃
   ┃ status             ┃ booked                           ┃
   ┗━━━━━━━━━━━━━━━━━━━━┻━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

### 9.4 DWD note

Without Domain-Wide Delegation on the SA, `events.insert` with
`attendees=[…]` returns HTTP 403. Default `JURINEX_VOICE_CALENDAR_ALLOW_ATTENDEES=false`
suppresses the attendees array (event still created, no invite email).
Flip to `true` only after enabling DWD in Google Workspace Admin Console.

---

## 10. Live human transfer

`transfer_call` ([`transfer_tools.py`](app/tools/transfer_tools.py)) —
two routing modes, both backed by `voice_agent_transfer_configs`:

| Mode | Source | Behaviour |
|---|---|---|
| **Static** | `static_destination` (E.164) | Dials that exact number. `destination_phone` arg is ignored. |
| **Dynamic** | `destination_prompt` (free text) | The system instruction renders the prompt + an extracted "Allowed numbers (must match exactly): +91…, +91…" list. The model passes `destination_phone` matching one of those numbers. The tool validates membership and rejects hallucinations with a clear error so the model retries. |

### 10.1 Anti-hallucination guard

```
hallucinated:    ('+919999999999', not in routing list (+917499303475, +917875827092))
missing arg:     ('', dynamic routing requires destination_phone — pick one of: …)
messy format:    '+91 7875 827 092' → normalised to '+917875827092'
```

The model's tool prompt fragment renders all this verbatim, so a 10-number
admin routing rule is handled the same as a 2-number rule (regex extracts
all `+\d{6,15}` and the model picks one based on caller intent).

### 10.2 Caller pitch in Preeti's voice

Preeti speaks the dynamic pitch herself before the dial fires; the tool
is then called with `farewell=""` so Twilio skips its own static `<Say>`
(otherwise the caller hears a robotic Polly voice over the top of
Preeti's pitch). On a Twilio trial account caller hears the bundled
ad-preamble — that's a Twilio limit, not a code limit.

---

## 11. Agent hot-swap

`agent_transfer` ([`agent_transfer_tools.py`](app/tools/agent_transfer_tools.py)) —
hands the live caller off to a different voice agent (e.g. sales →
support) without disconnecting the Twilio leg.

Sequence inside [`twilio_media_stream._swap_agent`](app/realtime/twilio_media_stream.py):

1. Send the tool response to the OLD model so its turn closes.
2. Resolve the target bundle (by id or name).
3. Build a fresh system instruction.
4. `await old_gemini.close()`, cancel the consume task.
5. Open a NEW `GeminiLiveClient` with the target's `live_model` /
   `voice_name` / `temperature` / enabled tools.
6. Replace `self.bundle` + `self.session.gemini` + restart
   `_consume_gemini_events()`.
7. Prime the new agent with the handoff message so it speaks first.

Twilio Media Stream stays open the whole time — caller hears no hangup.

---

## 12. Outbound call scheduler

Polls the admin-owned `voice_call_schedules` table, dials due rows via
Twilio, and writes status back as the call progresses. Spec is in
[`docs/SCHEDULER.md`](docs/SCHEDULER.md). Implementation in
[`app/services/scheduler_service.py`](app/services/scheduler_service.py)
+ [`app/db/voice_call_schedules_repository.py`](app/db/voice_call_schedules_repository.py).

### 12.1 Lifecycle

```
on app start (when SCHEDULER_ENABLED=true):
   SchedulerService.start() → asyncio.create_task(_poll_loop)

every SCHEDULER_POLL_SECONDS (default 5):
   _tick_once():
      while inflight_dials < SCHEDULER_MAX_INFLIGHT (default 4):
         claim_due_row()  → BEGIN; SELECT … FOR UPDATE SKIP LOCKED LIMIT 1;
                             UPDATE … SET status='queued'; COMMIT;
         dispatch in own task:
            normalize phone (defence in depth)
            verify agent.status='active'
            CallService.place_outbound_for_schedule(schedule_id, agent_name, …)
            mark_dialing(schedule_id, twilio_call_sid)  gated on status='queued'

bridge teardown:
   mark_completed(schedule_id, call_id)
```

### 12.2 Status state machine

```
pending  → admin INSERT
   ↓
queued   ← scheduler claim
   ↓
in_progress  ← scheduler mark_dialing  (gated, admin cancel wins race)
   ↓
completed / no_answer (terminal) / failed (terminal)
   ↓
or → re-queue: status=pending, scheduled_at += 15min × 2^(attempts-1) (cap 60min)
```

### 12.3 Console output per row

```
INFO scheduler.claim                     id=… → +91…
🗄️  UPDATE (status=queued) → voice_call_schedules        ← Rich table
🚨 SCHEDULER DIAL                        agent / to / attempt 1 of 3 / notes
🛠️ OUTBOUND CALL PLACED (SCHEDULER)      SID=CAxxxx Status=queued
INFO scheduler.dial.dispatched           sid=CAxxxx attempts=1
🗄️  UPDATE (status=in_progress) → voice_call_schedules

(call rings, conversation, ends — bridge teardown runs)

INFO scheduler.complete                  id=… call_id=…
🗄️  UPDATE (status=completed) → voice_call_schedules
```

On admin cancel between claim and dial: `mark_dialing` gate returns 0
rows → `scheduler.dial.cancelled` warning, no Twilio call placed.

### 12.4 Concurrency

`SCHEDULER_MAX_INFLIGHT` (default 4) caps how many Twilio dials can be
mid-flight per process. The DB layer (`FOR UPDATE SKIP LOCKED`) is
multi-worker safe — running 2+ uvicorn workers will not double-dial.

---

## 13. Post-call extraction + enrichment

[`post_call_service.py`](app/services/post_call_service.py) runs after
every call's `_teardown` (best-effort — exceptions never block teardown).

1. Pull enabled fields from `agent_builder.post_call_extraction[*]`.
2. Pick the model (`agent_builder.post_call_model`, default
   `gemini-2.5-flash`).
3. Assemble transcript from `call_messages` (one line per turn).
4. Call Gemini in a worker thread with a JSON-only prompt; coerce + zero-fill the response to the declared schema.
5. Write `voice_post_call_extractions` (running → completed/failed).
6. UPSERT `voice_call_enrichments` keyed on `call_id` with:
   - `successful` (from `call_successful` boolean)
   - `preferred_language`
   - `recording_url` / `recording_gcs_uri`
   - `cost_usd` (see [§14](#14-pricing-per-call))
   - `transfer_requested` (set when `_terminate_reason == 'transfer'`)
   - `analysis` JSONB carrying the full extracted block + pricing

Smoke-tested live: 5-turn synthetic transcript → Gemini 2.5 Flash
returned `{call_summary, call_successful: true, user_sentiment: positive,
preferred_language: English}` in 4.7 s.

---

## 14. Pricing per call

[`pricing_service.py`](app/services/pricing_service.py) — looks up
`voice_model_pricing` by the agent's `live_model` and computes:

```
caller_min = caller_seconds / 60
agent_min  = agent_seconds  / 60
cost_usd = round(caller_min × input_audio_usd_per_minute
              + agent_min  × output_audio_usd_per_minute, 6)
total_min = caller_min + agent_min
cost_inr_estimate = round(total_min × inr_one_minute_total, 4)
```

Stamped into:

- `calls.raw_metadata.pricing` (full payload)
- `voice_call_enrichments.cost_usd` (scalar — for dashboard reports)
- `voice_call_enrichments.analysis.pricing` (full payload again)

For models without per-minute audio rates (e.g. `gemini-2.5-flash`
text-only), `cost_usd` is `None` and only `inr_one_minute_total` is
reported.

---

## 15. Voice catalogue validation

[`platform_voices_repository.py`](app/db/platform_voices_repository.py)
caches the `platform_voices` set for 5 minutes. At `_on_start`, the
bridge checks the bundle's `voice_name` against the active set — on
miss, logs at ERROR with the catalogue size + sample, and falls back to
`Aoede`. Without this check an unknown voice silently kills the live
session with WS 1008 a few seconds in.

```
INFO  platform_voices.loaded     count=23 sample=[Kore, Achernar, Schedar, …]
DEBUG voice.validation.ok        voice=Achernar confirmed (catalogue size=23)
ERROR voice.validation.failed    voice='Random' not in platform_voices …
```

---

## 16. Observability

Every meaningful state change emits two artefacts:

1. **Structured dataflow log** with a key prefix (`gemini.session.open`,
   `agent.bundle.loaded`, `tool.dispatch`, `scheduler.claim`,
   `calendar.book.bad_iso`, `post_call.done`, `pricing.computed`, …).
   Easily greppable.
2. **Rich panel** for human-readable lifecycle events
   (`CALL STARTED`, `TOOL CALL FROM MODEL`, `CALENDAR BOOK`,
   `BOOKING SUCCEEDED`, `SCHEDULER DIAL`, `GEMINI RESUME`, …).

Plus, every persisted row in the audit tables prints as a Rich
**column / value** table via `log_db_row(...)`
([`rich_console.py:render_db_row_table`](app/observability/rich_console.py)).
Currently wired for `voice_calendar_bookings` and `voice_call_schedules`;
extend to other repositories with one import + call.

### 16.1 Log key glossary (selected)

| Prefix | Meaning |
|---|---|
| `twilio.media.*` | Media-stream protocol events |
| `twilio.twiml.generated` | TwiML returned to a webhook |
| `gemini.session.*` / `gemini.config.*` / `gemini.resume.*` | Live session lifecycle + auto-resume |
| `gemini.transcript.*` | Input/output transcripts |
| `agent.bundle.*` | DB bundle load |
| `agent.routing.selected` | Multi-agent routing pick |
| `agent.swap.*` | Hot-swap to a different voice agent |
| `prompt.assembled` / `prompt.source` | System instruction build |
| `prompts.cache.{hit,miss,invalidated}` | Fragment + tool-prompt TTL cache |
| `tool.dispatch` / `tool_exec.{pending,completed,failed}` | Tool dispatch + audit |
| `kb.*` | KB search + shadow-RAG injection |
| `calendar.*` | Calendar tools (parse, retry, verify) |
| `scheduler.*` | Outbound poller |
| `post_call.*` | Extraction + enrichment |
| `pricing.*` | Per-call cost stamp |
| `platform_voices.*` / `voice.validation.*` | Catalogue check |
| `watchdog.*` | Silence + max-duration + Gemini failure |
| `recorder.*` / `gcs.*` | Recording + upload |
| `debug_event.*` | voice_debug_events writes |

---

## 17. HTTP API surface

| Path | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | – | Liveness + app name |
| `/health/...` | GET | – | Health checks |
| `/twilio/incoming-call` | POST | Twilio signature | Inbound webhook → TwiML Stream |
| `/twilio/outbound-answer` | POST | Twilio signature | Outbound answer URL → TwiML Stream (carries `schedule_id` + `agent_name`) |
| `/twilio/call-status` | POST | Twilio signature | Status callbacks (initiated/ringing/answered/completed) |
| `/twilio/media-stream` | WebSocket | – | The audio bridge |
| `/admin/outbound-call` | POST | `X-Admin-API-Key` | Manual outbound dial (`OutboundCallRequest`) |
| `/admin/calls` / `/admin/calls/{id}` | GET | admin | List + drill into calls |
| `/admin/tickets` | GET | admin | List support tickets |
| `/admin/debug-events` | GET | admin | List call_debug_events |
| `/admin/voice/reload` | POST | admin | Drop the prompt-fragment + tool-prompt TTL cache |
| `/debug/...` | various | admin | Simulator, manual KB search, etc. |

Auth: every `/admin/...` route is gated by the
`require_admin_api_key` dependency — pass `X-Admin-API-Key:
$ADMIN_API_KEY` (or the existing JWT/bearer token if your front-end
sets one).

---

## 18. Operational runbook

### 18.1 First-time deploy

1. `cp .env.example .env`, fill secrets.
2. Decide single-agent (`KB_AGENT_NAME=preeti`) vs multi-agent (use
   Twilio Studio per-number routing).
3. `alembic upgrade head` (only creates this service's own tables —
   admin tables are owned by the admin app).
4. Set Twilio number's voice webhook to
   `<PUBLIC_BASE_URL>/twilio/incoming-call`.
5. `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
6. Verify `🤖  GEMINI SESSION STARTED` appears on a test call.

### 18.2 Admin edited a prompt — push it live without a redeploy

```
curl -X POST -H "X-Admin-API-Key: $ADMIN_API_KEY" \
     https://your-bridge/admin/voice/reload
```

Or wait ≤60s (the cache TTL). Next call uses the new template.

### 18.3 Enable outbound scheduler

```dotenv
SCHEDULER_ENABLED=true
SCHEDULER_POLL_SECONDS=5
SCHEDULER_MAX_INFLIGHT=4
```

Restart. `🛠️ SCHEDULER STARTED` panel appears at boot.

### 18.4 Calendar invites stopped working

Likely you flipped `JURINEX_VOICE_CALENDAR_ALLOW_ATTENDEES=true` without
DWD enabled. Symptom: `BOOKING FAILED` with HTTP 403. Either complete
DWD setup or flip back to `false`.

### 18.5 Common failure reasons in the console

| Panel | Meaning | Fix |
|---|---|---|
| `prompt.source: static fallback` (warning) | DB bundle missing/inactive | Check `voice_agents.status='active'` for the requested name |
| `voice.validation.failed` | Bundle's voice not in catalogue | Pick a name from `platform_voices` (or accept the `Aoede` fallback) |
| `BOOKING UNCONFIRMED` (yellow) | Insert succeeded but post-insert verify didn't find the event | Calendar permissions / quota — investigate via Google Workspace logs |
| `BOOKING FAILED` (red) | Insert failed all retries | Look at `voice_calendar_bookings.metadata.error` |
| `gemini.resume.exhausted` | 3 reconnects failed | Check API key / quota / model availability |
| `scheduler.failed` (red `voice_call_schedules`) | Twilio rejected the dial | Read `last_error`; on trial accounts, 429 means concurrency cap |

---

## 19. Testing & smoke checks

```bash
# Compile every touched module
.venv/bin/python -m py_compile $(find app -name "*.py")

# Boot smoke
.venv/bin/python -c "import app.main; print('OK', app.main.app.title)"

# Bundle loader
.venv/bin/python -c "
import asyncio
from app.db.database import session_scope
from app.db.voice_agent_repository import VoiceAgentRepository
async def main():
    async with session_scope() as s:
        b = await VoiceAgentRepository(s).load_active_bundle('preeti')
        print(b.name, b.live_model, b.voice_name, b.enabled_function_keys)
asyncio.run(main())"

# Pytest
pytest -q
```

End-to-end live tests already verified during build:
- bundle load + system-instruction assembly
- prompt cache miss/hit/invalidate
- voice_tool_executions round-trip
- agent_transfer (4 sub-tests)
- post-call extraction with live Gemini 2.5 Flash
- voice_debug_events write
- pricing math (manual cross-check)
- platform_voices catalogue
- calendar_check + calendar_book against the real calendar
- transfer dynamic-routing destination validation
- scheduler claim → dial → complete + re-queue + exhaust + failed paths

---

## 20. File layout

```
Jurinex_call_agent/
├── alembic.ini  alembic/                          ← own-DB migrations
├── docker-compose.yml  Dockerfile  Makefile  pytest.ini
├── docs/
│   ├── ARCHITECTURE.md  DATAFLOW.md
│   ├── PHASE3.md                                  ← coverage gaps now closed
│   └── SCHEDULER.md                               ← outbound queue contract
├── scripts/                                       ← one-shot ops scripts
├── app/
│   ├── main.py  lifecycle.py  config.py
│   ├── api/
│   │   ├── twilio_routes.py     ← inbound + outbound webhooks + WS
│   │   ├── admin_routes.py      ← /admin/... + /admin/voice/reload
│   │   ├── debug_routes.py  health_routes.py
│   ├── db/
│   │   ├── database.py  models.py  schemas.py  repositories.py
│   │   ├── voice_agent_repository.py
│   │   ├── prompt_fragments_repository.py
│   │   ├── platform_voices_repository.py
│   │   ├── voice_tool_executions_repository.py
│   │   ├── voice_calendar_bookings_repository.py
│   │   ├── voice_post_call_repository.py          ← extractions + enrichments
│   │   ├── voice_debug_events_repository.py
│   │   └── voice_call_schedules_repository.py
│   ├── observability/
│   │   ├── logger.py            ← log_dataflow / log_event_panel / log_db_row
│   │   ├── rich_console.py      ← render_event_panel / render_db_row_table
│   │   └── trace_context.py
│   ├── prompts/                 ← static fallback persona (used in degraded mode)
│   ├── realtime/
│   │   ├── twilio_media_stream.py      ← the bridge core
│   │   ├── gemini_live_client.py       ← Gemini WS + tool declarations + auto-resume
│   │   ├── audio_codec.py              ← μ-law ↔ PCM resampling
│   │   ├── call_recorder.py            ← timeline-mixed WAV
│   │   ├── greeting_loader.py          ← pre-rendered WAV cache
│   │   ├── session_manager.py  events.py
│   ├── services/
│   │   ├── system_instruction_builder.py
│   │   ├── tool_dispatcher.py
│   │   ├── scheduler_service.py        ← outbound poller
│   │   ├── post_call_service.py        ← Gemini-driven JSON extraction
│   │   ├── pricing_service.py
│   │   ├── google_calendar.py          ← Calendar v3 over httpx
│   │   ├── kb_search.py
│   │   ├── call_service.py  summary_service.py  transcript_service.py
│   │   ├── ticket_service.py  compliance_service.py  gcs_uploader.py
│   ├── tools/
│   │   ├── kb_tools.py
│   │   ├── transfer_tools.py           ← dynamic destination resolver
│   │   ├── calendar_tools.py           ← strict TZ + retry + verify
│   │   ├── agent_transfer_tools.py     ← hot-swap
│   │   ├── ticket_tools.py  escalation_tools.py  end_call_tools.py
│   │   └── customer_tools.py  case_tools.py
│   ├── utils/
│   │   ├── phone.py             ← normalize_e164
│   │   ├── time_utils.py  security.py
│   └── static/                  ← greeting.wav (μ-law cached at startup)
└── tests/                       ← pytest
```

---

## 21. Phase history

| Phase | Scope |
|---|---|
| **Phase 0** | Initial Twilio Media Streams + Gemini Live + GCS recording + RAG + ticket/escalation. README §3-§8 here. |
| **Phase 1 — DB-driven config** | Bundle loader, fragment repo with TTL cache, system-instruction builder, voice_tool_executions writer, dynamic transfer routing. Replaces all .env-based agent config with admin-DB driven config. |
| **Phase 2 — Calendar tools** | google_calendar.py, calendar_check + calendar_book with 3-layer reliability, voice_calendar_bookings audit. |
| **Phase 3 — Admin coverage gaps** | All 9 items in [docs/PHASE3.md](docs/PHASE3.md): agent_transfer hot-swap, post-call extraction, voice_debug_events writer, live session auto-resume, multi-agent routing, admin reload endpoint, voice_model_pricing lookup, platform_voices validation, calendar DWD note. |
| **Outbound call scheduler** | voice_call_schedules poller + dial pipeline, full state machine, Rich table console panels. Spec: [docs/SCHEDULER.md](docs/SCHEDULER.md). |

The bridge today is fully DB-driven, multi-agent capable, has live human
transfer with dynamic routing, books real Google Calendar demos with
verification, runs post-call extraction, stamps cost per call, validates
voices against the catalogue, auto-resumes Gemini WS drops, and dials
admin-scheduled outbound rows on a poller — all observable in the Rich
console with structured dataflow logs.

---

## License

Internal — Nexintel AI / Jurinex.




curl -X POST http://localhost:8000/admin/outbound-call   -H "Content-Type: application/json"   -H "X-Admin-API-Key: jurinex_admin_demo_key"   -d '{"to_phone_number":"+917875827092","customer_name":"Demo User","language_hint":"Hindi","reason":"Demo"}'


.venv/bin/uvicorn app.main:app --reload