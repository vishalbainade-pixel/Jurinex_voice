# Jurinex_call_agent — Preeti, the multilingual support voice agent

Production-grade Python backend for a multilingual (English / Hindi / Marathi)
AI voice customer-support agent named **Preeti**, for the **Jurinex** platform.

It bridges:

- **Twilio Media Streams** for telephony (with μ-law ↔ PCM resampling)
- **Gemini Live API** for realtime conversation + transcription
- **Cloud SQL PostgreSQL + pgvector** for transcripts, tickets, escalations,
  debug events, and the RAG knowledge base
- **GCS** for per-call timeline-mixed WAV recordings
- **Twilio `<Dial>` bridge** for live human handoff to a support agent
- **FastAPI + Rich** for the HTTP surface and console observability

> Preeti is grounded in your Jurinex product documentation via RAG against
> the admin-owned `kb_chunks` table — see [§8](#8-product-knowledge-base--human-handoff).
> When she isn't confident, she transfers the live caller to
> `SUPPORT_ADMIN_PHONE` (Twilio bridges the two legs into a 3-way call).
> Set `DEMO_MODE=true` to short-circuit Gemini with a deterministic
> simulator for end-to-end testing without telephony or a live API key.

---

## 1. What this project does

```
Caller → Twilio number → /twilio/incoming-call → TwiML <Stream> →
   /twilio/media-stream (WebSocket) → GeminiLiveClient → Preeti speaks →
   Transcript + Tickets + Escalations + Summary saved in PostgreSQL
```

It also supports admin-initiated **outbound** calls via Twilio's REST API.

## 2. Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/DATAFLOW.md`](docs/DATAFLOW.md). Short version:

```
api/  →  services/  →  db/repositories  ──> Cloud SQL (calls, tickets, KB, …)
            │                    ↘
            │                     tools/  ──> search_knowledge_base
            ▼                                 transfer_to_human_agent
        realtime/  ←  Twilio Media Streams         create_support_ticket
            │      ←  Gemini Live API              escalate_to_human, end_call
            ▼
       call_recorder/  ──>  GCS (recording.wav per call)
```

## 3. Local setup

```bash
git clone <this repo> jurinex
cd jurinex
cp .env.example .env       # then edit values

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Run with Docker Compose (recommended)

```bash
docker compose up -d --build
# app at http://localhost:8000
# postgres at 5432
docker compose logs -f app
```

The app container runs `alembic upgrade head` before launching uvicorn.

## 5. Run locally (without Docker)

If you point `DATABASE_URL` at the Cloud SQL instance from `.env`, no local
Postgres is needed:

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Otherwise, start a local Postgres first:

```bash
docker compose up -d postgres
alembic upgrade head
uvicorn app.main:app --reload
```

## 6. Environment variables

The `.env` file is the **single configuration surface** for the whole app —
hosts, credentials, model choices, and runtime safety knobs all live here.
The app reads it on startup via [`app/config.py`](app/config.py) (Pydantic
Settings), so changing a value requires a uvicorn restart, never a code edit.

Start by copying the template and filling in real values:

```bash
cp .env.example .env
```

### 6.1 App identity & logging

| Var | What it is | Why it exists |
| --- | --- | --- |
| `APP_NAME` | Display name shown in logs & startup panel | Useful when running multiple agents from one host |
| `APP_ENV` | `development` / `staging` / `production` | Enables stricter checks in prod (e.g. failing closed when `ADMIN_API_KEY` is unset) |
| `DEBUG` | `true` / `false` | When `true`, dataflow logs include payload previews |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | Controls Rich console verbosity |
| `DEMO_MODE` | `true` / `false` | `true` short-circuits Gemini with a deterministic Hindi/Marathi/English simulator — lets you test DB + tool flows without Twilio or a live API key |
| `PUBLIC_BASE_URL` | The HTTPS URL Twilio reaches you on | Used to build the `wss://…/twilio/media-stream` URL embedded in TwiML. **Must match what Twilio dials** (your ngrok URL locally, your Cloud Run URL in prod) |

### 6.2 Twilio (telephony)

| Var | What it is | Why it exists |
| --- | --- | --- |
| `TWILIO_ACCOUNT_SID` | Twilio account ID (`AC…`) | Auth for the Twilio REST client used to place outbound calls and to hang up legs |
| `TWILIO_AUTH_TOKEN` | Twilio account auth token | Same — pair with the SID. Treat as a secret |
| `TWILIO_PHONE_NUMBER` | Your Twilio-owned number (E.164, e.g. `+18159348556`) | The "from" number on outbound calls and the inbound number callers will dial |

### 6.3 Gemini (the AI brain)

| Var | What it is | Why it exists |
| --- | --- | --- |
| `GOOGLE_API_KEY` | A real Google AI Studio key (starts with `AIza…`) | Authenticates the `google-genai` Live SDK. Ephemeral OAuth tokens (`AQ.…`) do **not** work — the Live API will reject them with WS close 1008 |
| `GEMINI_API_KEY` | Same key, alternate name | Some setups read this name; the app accepts either |
| `GEMINI_MODEL` | Model ID, e.g. `gemini-3.1-flash-live-preview` | Lets you switch between Live model versions without code changes. Fall back to `gemini-2.0-flash-live-001` if the new preview model isn't enabled on your project |
| `GEMINI_VOICE` | Prebuilt voice name: `Aoede` / `Charon` / `Kore` / `Puck` / `Fenrir` | Preeti's voice. Change voice with one env edit + restart — no code change |

### 6.4 Database

| Var | What it is | Why it exists |
| --- | --- | --- |
| `DATABASE_URL` | Async SQLAlchemy URL: `postgresql+asyncpg://user:pass@host:5432/db` | Used by the running app (async I/O via asyncpg) |
| `SYNC_DATABASE_URL` | Sync URL: `postgresql+psycopg2://user:pass@host:5432/db` | Alembic doesn't speak asyncpg, so migrations need a separate sync URL pointing at the same database |

Both URLs must point at the **same** Postgres instance. The split is purely a
driver concern.

### 6.5 Auth

| Var | What it is | Why it exists |
| --- | --- | --- |
| `SECRET_KEY` | Generic app secret | Reserved for future signed-token / cookie usage |
| `ADMIN_API_KEY` | Token required in the `X-Admin-API-Key` header | Protects `/admin/*` (outbound calls, list calls, list tickets, debug events) from open-internet abuse |

### 6.6 Call lifecycle controls (auto-hangup safety net)

These are env-tuned knobs that decide **when a call ends automatically**.
Without them a hung session could keep a Twilio leg open indefinitely
(burning telephony minutes) or leave the caller hearing dead air after a
Gemini failure.

| Var | What it is | Why it exists |
| --- | --- | --- |
| `SILENCE_TIMEOUT_SECONDS` | After this many seconds of detected caller silence (RMS-gated, not raw frames), Preeti is asked to politely say goodbye and the line is dropped | Prevents a caller who walked away from holding the line forever |
| `MAX_CALL_DURATION_SECONDS` | Hard cap on a single call's length | Runaway-cost safety net — if anything else fails, the call still ends here |
| `AUTO_HANGUP_ON_GEMINI_FAILURE` | `true` → if the Gemini Live session dies, replace the active TwiML with a fallback `<Say>` + `<Hangup/>`. `false` → just log the failure and leave the line open | Keeps callers from sitting in silence after a model/network failure |
| `FAREWELL_GRACE_SECONDS` | How long to let Preeti's spoken goodbye play before actually disconnecting | Without this, the audio is cut mid-sentence |
| `TECHNICAL_FAILURE_MESSAGE` | The English line spoken when Gemini is dead and we can't generate a localized goodbye | Keeps the caller informed instead of dropping them silently |

To **disable** any of these at runtime, raise the limit (`SILENCE_TIMEOUT_SECONDS=99999`)
or flip the boolean (`AUTO_HANGUP_ON_GEMINI_FAILURE=false`) and restart uvicorn.

### 6.7 GCS call recordings

We tap both audio sides as they flow through the realtime layer (caller μ-law
from Twilio, agent PCM from Gemini), buffer them in memory, and at call-end
upload three objects to a single GCS folder per call:

```
gs://<GCS_BUCKET>/YYYY-MM-DD/HH-MM-SS_<call_sid>/
  ├── recording.wav   mono PCM16 16 kHz — caller + agent on a single timeline
  └── metadata.json   call_sid, language, started_at, ended_at, durations,
                      terminate_reason, recording URIs
```

`recording.wav` is built by:
1. decoding the caller's μ-law and resampling to 16 kHz,
2. dropping each agent (Preeti) audio chunk into a silent buffer at the time
   it actually arrived from Gemini (so gaps between her turns stay silent),
3. sample-summing the two buffers (with automatic clipping).

The result is one listenable file you can scrub like any phone call.

The folder URI is also written into the `calls.raw_metadata` JSONB column so
admins can find it via `/admin/calls/{id}`.

| Var | What it is | Why it exists |
| --- | --- | --- |
| `GCS_RECORDINGS_ENABLED` | `true` / `false` | Master switch. Disable for local dev to avoid touching GCS at all |
| `GCS_BUCKET` | Bucket name (no `gs://`) | Where recordings land. The service account must have `roles/storage.objectAdmin` (or at least `objectCreator`) on this bucket |
| `GCS_PROJECT_ID` | GCP project ID (e.g. `nexintel-ai-summarizer`) — optional | Explicit billing/quota project for the Storage client. If unset, falls back to the `project_id` inside the service-account JSON. Useful when the bucket lives in a different project than the SA, or for unambiguous logs |
| `GCS_KEY_BASE64` | base64 of a service-account JSON, pasted directly | Easiest auth for Cloud Run / Docker — no file mount needed. Wins over `GOOGLE_APPLICATION_CREDENTIALS` if both are set |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to a service-account JSON key | Standard GCP auth. Leave both blank on Cloud Run / GKE if Workload Identity is attached to the runtime service account |

#### Encoding a service-account JSON to `GCS_KEY_BASE64`

```bash
base64 -w0 ~/jurinex-sa.json   # Linux
base64       ~/jurinex-sa.json # macOS
```

Paste the single-line output as the value of `GCS_KEY_BASE64`.

#### Bucket setup (one-time)

```bash
# Create the bucket (single-region for lower latency from your runtime)
gcloud storage buckets create gs://jurinex-voice --location=asia-south1

# Service account with object-admin on this bucket only
gcloud iam service-accounts create jurinex-call-agent \
  --display-name="Jurinex call agent recorder"

gcloud storage buckets add-iam-policy-binding gs://jurinex-voice \
  --member="serviceAccount:jurinex-call-agent@<PROJECT>.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# Local dev: download a key
gcloud iam service-accounts keys create ~/jurinex-sa.json \
  --iam-account=jurinex-call-agent@<PROJECT>.iam.gserviceaccount.com

# Then in .env:
#   GOOGLE_APPLICATION_CREDENTIALS=/home/<you>/jurinex-sa.json
```

To **disable** uploading entirely, set `GCS_RECORDINGS_ENABLED=false` and
restart. The recorder will short-circuit and never touch GCS.

### 6.8 Knowledge base (RAG) — read against admin-owned KB tables

Preeti grounds her product answers in chunks the **admin app** ingests
into the shared `kb_documents` / `kb_chunks` tables (see [§8](#8-product-knowledge-base--human-handoff)).
This call agent only **reads** the KB and writes audit rows to
`kb_search_logs`.

| Var | What it is | Why it exists |
| --- | --- | --- |
| `KB_ENABLED` | `true` / `false` | Master switch. When `false`, `search_knowledge_base` short-circuits and Preeti will fall back to transfer |
| `KB_AGENT_NAME` | The `voice_agents.name` row this call agent represents (default `preeti`) | Scopes searches: only documents whose `agent_id` matches this agent (or are global) are returned |
| `KB_EMBEDDING_MODEL` | Embedding model used for query side (must match the model the admin app indexed with) | Default `gemini-embedding-001` to match the admin pipeline |
| `KB_EMBEDDING_DIM` | Dimensionality (must match `kb_chunks.embedding`) | Fixed at `768` for `gemini-embedding-001` matryoshka-truncated |
| `KB_SEARCH_K` | How many chunks to retrieve per search | Default `5` |
| `KB_MIN_SCORE` | Cosine-similarity floor below which Preeti must hand off | Default `0.60`. Below this, the tool returns `confident: false` and the prompt instructs Preeti to call `transfer_to_human_agent` |

### 6.9 Human-agent transfer (Twilio Dial bridge)

| Var | What it is | Why it exists |
| --- | --- | --- |
| `SUPPORT_ADMIN_PHONE` | E.164 number Twilio will dial when Preeti escalates (default `+917885820020`) | The human support agent on the other end. **On Twilio trial accounts this number must be on the Verified Caller IDs list, otherwise the `<Dial>` will fail** |

### 6.10 Quick reference — minimum vars to actually run a call

```env
PUBLIC_BASE_URL=https://<your-ngrok>.ngrok-free.app
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+1...
GOOGLE_API_KEY=AIza...
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_VOICE=Aoede
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db
SYNC_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/db
ADMIN_API_KEY=some_random_string
DEMO_MODE=false
```

Everything else has sane defaults baked into [`app/config.py`](app/config.py).

## 7. Database schema

The app uses **7 tables** in PostgreSQL (Cloud SQL or local). The ORM models
live in [`app/db/models.py`](app/db/models.py) and the initial migration is
[`alembic/versions/20260427_0001_initial.py`](alembic/versions/20260427_0001_initial.py).

### 7.1 At-a-glance

| Table | What it stores | When it's written | When it's read | Status |
| --- | --- | --- | --- | --- |
| `customers` | One row per phone number that ever called or was called | `lookup_customer`, `create_support_ticket`, outbound dial, demo simulator, seed | Linked to from `calls`/`support_tickets` via FK | ✅ heavily used |
| `calls` | One row per call leg — direction, status, language, summary, GCS recording URI | `_on_start` (inbound), `place_outbound` (outbound), demo simulator | `/admin/calls`, `/admin/calls/{id}`, summary builder | ✅ heavily used |
| `call_messages` | Per-turn transcript: who said what, when, in which language | Demo simulator + every Gemini transcription event (caller side via `input_audio_transcription`, agent side via `output_audio_transcription`) | `/admin/calls/{id}`, `SummaryService.build_summary` | ✅ used (transcription enabled in Live config; SDK degrades gracefully on older versions) |
| `support_tickets` | One row per ticket Preeti opens during a call | `create_support_ticket` tool | `/admin/tickets` | ✅ used |
| `escalations` | One row per "hand to a human" event | `escalate_to_human` tool | (no admin endpoint yet) | ✅ used |
| `agent_tool_events` | Audit log: every tool the agent invoked, with input/output/success | `tool_dispatcher` + each tool's success path + each tool's error path | (no admin endpoint yet — query manually for debugging) | ✅ used |
| `call_debug_events` | Persistent dataflow events linked to a call | `log_dataflow(...)` auto-persists any stage matching `_PERSIST_PREFIXES` (twilio.media.*, gemini.session, watchdog.*, tool.*, gcs.*, etc.) — fire-and-forget background task, never blocks the realtime path | `/admin/debug-events` | ✅ used |

### 7.2 Per-table detail

#### `customers`
The single source of truth for "who called us". Idempotent on `phone_number`
(unique index), so repeated lookups for the same number reuse the same row.

| Column | Why it exists |
| --- | --- |
| `id` (UUID) | Primary key — referenced by `calls.customer_id`, `support_tickets.customer_id` |
| `phone_number` | E.164-normalized, unique. Index for fast lookup on every inbound call |
| `name` | Optional display name (set during outbound dial or via `create_support_ticket`) |
| `email` | Optional, indexed for future support-portal lookups |
| `preferred_language` | `English` / `Hindi` / `Marathi` — set when the caller picks a language; lets future calls open in their preferred language |
| `created_at`, `updated_at` | Standard timestamps |

#### `calls`
Every call (inbound, outbound, or simulated) gets exactly one row. This is the
"call ledger" — admin endpoints, summaries, recordings, and ticket links all
fan out from here.

| Column | Why it exists |
| --- | --- |
| `id` (UUID) | Primary key |
| `twilio_call_sid` | Twilio's `CAxxxx` ID, unique — joins us to Twilio's own call records |
| `customer_id` (FK) | Whose call this is (nullable for unknown numbers) |
| `customer_phone`, `twilio_from`, `twilio_to` | Phone-number trail captured from Twilio webhooks. Customer phone is also indexed for "show me all calls from +91…" |
| `direction` | `inbound` / `outbound` |
| `status` | `started` / `in_progress` / `completed` / `failed` — current call lifecycle state |
| `language` | The language Preeti settled on after the caller's first reply |
| `issue_type` | High-level category if the agent classified the call |
| `resolution_status` | `resolved` / `unresolved` / `escalated` / `unknown` |
| `started_at`, `ended_at`, `duration_seconds` | Wall-clock + duration |
| `summary` | Built by `SummaryService.build_summary` at teardown |
| `sentiment` | Reserved — populated when sentiment analysis is added |
| `created_ticket_id` | Convenience pointer if exactly one ticket was opened on this call |
| `raw_metadata` (JSONB) | Free-form bag — currently holds `{"recording": {"folder": "gs://…", "recording": "gs://…/recording.wav", "metadata": "gs://…/metadata.json"}}` so admins can find the GCS recording without a separate table |

#### `call_messages`
The per-turn transcript of a call. One row per utterance.

| Column | Why it exists |
| --- | --- |
| `id` (UUID) | Primary key |
| `call_id` (FK, indexed) | Which call this turn belongs to |
| `speaker` | `customer` / `agent` / `system` / `tool` |
| `language` | Language of this specific turn (allows mid-call language switch detection) |
| `text` | The actual transcript text |
| `audio_event_id` | Reserved — pointer to a specific Gemini audio event when we add fine-grained alignment |
| `timestamp` | When the turn was uttered |
| `raw_payload` (JSONB) | Optional — full provider payload if we ever need to debug a transcript |

Real-call transcripts are captured by enabling Gemini's
`input_audio_transcription` (caller) and `output_audio_transcription`
(agent) in `LiveConnectConfig`. The handler routes those events to
`TranscriptService.save_message`. If the installed `google-genai` version
doesn't support these fields, the session still opens — only the
`call_messages` table goes empty (and a one-line warning is logged).

#### `support_tickets`
What Preeti opens via the `create_support_ticket` tool — the durable artifact
of "you called, here's the case the team will follow up on".

| Column | Why it exists |
| --- | --- |
| `id` (UUID) | Primary key |
| `ticket_number` (unique, indexed) | Human-friendly reference, format `JX-YYYYMMDD-NNNN`, generated atomically by `next_ticket_number()` |
| `customer_id`, `call_id` | Where the ticket came from |
| `issue_type` (indexed) | e.g. `OTP_NOT_RECEIVED`, `LOGIN_ISSUE`, `PAYMENT_QUERY` — indexed so analytics queries by category are fast |
| `issue_summary` | One-paragraph human description |
| `priority` | `low` / `normal` / `high` / `urgent` |
| `status` | `open` / `in_progress` / `resolved` / `escalated` |
| `created_at`, `updated_at` | Standard timestamps |

#### `escalations`
A separate audit trail for "this conversation needs a human", deliberately
distinct from `support_tickets` — an escalation may or may not produce a
ticket, and a ticket may or may not get escalated.

| Column | Why it exists |
| --- | --- |
| `id` (UUID) | Primary key |
| `call_id` (FK) | The call that triggered the escalation |
| `ticket_id` (FK, nullable) | Optional link if a ticket was also opened |
| `reason` | Free-text — why escalation was needed |
| `assigned_team` | e.g. `tier-2-support`, `legal`, `billing` |
| `status` | `pending` / `assigned` / `resolved` |
| `created_at`, `updated_at` | Standard timestamps |

#### `agent_tool_events`
The "what did the AI do" audit table. Every time Preeti invokes a tool, we
write one row whether it succeeded or not. This is the single most useful
table for debugging the agent's behaviour after a call.

| Column | Why it exists |
| --- | --- |
| `id` (UUID) | Primary key |
| `call_id` (FK) | Which call the tool was invoked from |
| `tool_name` | e.g. `create_support_ticket`, `lookup_customer`, `escalate_to_human`, `end_call` |
| `input_json` (JSONB) | Exactly what the model passed to the tool |
| `output_json` (JSONB) | What the tool returned (ticket number, escalation ID, etc.) |
| `success` | Boolean — distinguishes successful tool calls from failed ones |
| `error_message` | Set on failure; null on success |
| `created_at` | When the tool fired |

#### `call_debug_events`
A persistent dataflow log keyed to a call — useful for after-the-fact
debugging when console logs are gone. `log_dataflow(...)` automatically
persists any stage matching `_PERSIST_PREFIXES` in
[`app/observability/logger.py`](app/observability/logger.py): twilio media
start/stop, gemini session lifecycle, transcripts, watchdog firings, tool
dispatches, GCS uploads, and call summaries. Persistence is fire-and-forget
on a background task so it never blocks the realtime path.

| Column | Why it exists |
| --- | --- |
| `id` (UUID) | Primary key |
| `call_id` (FK, nullable) | Some events fire before the call row exists |
| `twilio_call_sid` (indexed) | Lets you find debug events even when `call_id` is null |
| `event_type` (indexed) | `twilio` / `gemini` / `tool` / `watchdog` / etc. |
| `event_stage` (indexed) | The specific stage, e.g. `media.start`, `session.open` |
| `message` | Human-readable line |
| `payload` (JSONB) | Optional structured payload |
| `created_at` | Timestamp |

To add a new persistable stage, extend `_PERSIST_PREFIXES` in
[`logger.py`](app/observability/logger.py), or pass `persist=True` directly
to `log_dataflow(...)` for one-off events.

### 7.3 ER overview (text)

```
            customers (1) ──────┬─< calls (N) >─┬── (N) call_messages
                                │                │
                                │                ├── (N) agent_tool_events
                                │                │
                                │                ├── (N) escalations  ──> support_tickets
                                │                │
                                │                └── (N) call_debug_events
                                │
                                └─< support_tickets (N)
```

## 8. Product knowledge base & human handoff

Preeti is grounded in your Jurinex product documentation via **retrieval-augmented
generation (RAG)** and can hand the call off to a human when she's not confident
or the issue is account-specific.

### 8.1 Architecture

```
┌─────────────────────────┐         ┌──────────────────────────────┐
│  Admin app (sibling)    │  writes │  Shared Cloud SQL DB         │
│  /admin/kb/upload       │ ──────► │   voice_agents               │
│  • PDF/DOCX → text      │         │   kb_documents               │
│  • chunks ~500 tokens   │         │   kb_chunks (vector(768))    │
│  • embeds w/ gemini-    │         │   kb_search_logs             │
│    embedding-001        │         │   voice_debug_events         │
└─────────────────────────┘         └──────────────┬───────────────┘
                                                   │ reads
                                                   ▼
            ┌──────────────────────────────────────────────────────┐
            │  Jurinex_call_agent (this app)                       │
            │  Preeti calls search_knowledge_base(query, k)        │
            │  → embed query (gemini-embedding-001 RETRIEVAL_QUERY)│
            │  → cosine ANN on kb_chunks                           │
            │  → INSERT kb_search_logs row                         │
            │  → return ranked chunks + `confident` flag           │
            └──────────────────────────────────────────────────────┘
```

The admin app **owns ingestion** (chunking, embedding, GCS, `status`).
This app **owns retrieval and the live call**. Both vector spaces match
because both use the same model + 768-dim cosine metric.

### 8.2 The two tools Preeti can call

These are declared in [`gemini_live_client.py`](app/realtime/gemini_live_client.py)
inside `LiveConnectConfig.tools`, dispatched in [`tool_dispatcher.py`](app/services/tool_dispatcher.py),
and instructed in [`jurinex_preeti_prompt.py`](app/prompts/jurinex_preeti_prompt.py) §5.

#### `search_knowledge_base(query, k=5)`
- File: [`app/tools/kb_tools.py`](app/tools/kb_tools.py),
  service: [`app/services/kb_search.py`](app/services/kb_search.py)
- Embeds the query via `gemini-embedding-001` (`task_type=RETRIEVAL_QUERY`,
  `output_dimensionality=768`).
- Runs the cosine SQL:
  ```sql
  SELECT c.id, c.text, c.heading_path, c.document_id,
         d.title AS document_title,
         1 - (c.embedding <=> CAST(:q AS vector)) AS score
  FROM kb_chunks c
  JOIN kb_documents d ON d.id = c.document_id
  WHERE d.status = 'ready'
    AND (CAST(:agent AS uuid) IS NULL
         OR d.agent_id = CAST(:agent AS uuid)
         OR d.agent_id IS NULL)
  ORDER BY c.embedding <=> CAST(:q AS vector)
  LIMIT :k;
  ```
- Logs: `kb_search_logs` row + `agent_tool_events` row + a `🗄️ KB SEARCH` Rich panel.
- Returns: `{ confident, top_score, results: [{ score, document, section, text }] }`
  trimmed for the LLM. `confident` is `true` only when `top_score >= KB_MIN_SCORE`.

#### `transfer_to_human_agent(reason, farewell?)`
- File: [`app/tools/transfer_tools.py`](app/tools/transfer_tools.py).
- Builds new TwiML:
  ```xml
  <Response>
    <Say voice="alice">Connecting you to a Jurinex support agent. Please hold.</Say>
    <Dial callerId="+18159348556">+917885820020</Dial>
  </Response>
  ```
- Calls `client.calls(call_sid).update(twiml=…)` so Twilio replaces our
  active call's TwiML with the bridge. Caller stays on the line; Twilio
  dials the admin and bridges them. Our media-stream WS closes cleanly.
- DB side-effects: `escalations` row, `agent_tool_events` row,
  `calls.resolution_status='escalated'`.
- The GCS recording keeps capturing both legs through the bridge.

### 8.3 How Preeti decides what to do

The system prompt (§5 *"Tools you must use"*) hard-codes the policy:

1. Any product/feature/pricing question → **must** call
   `search_knowledge_base` first. Answer **only** from returned chunks.
2. If `confident: false` or chunks don't cover the question → speak one
   short transfer line in the caller's language, then call
   `transfer_to_human_agent`.
3. Account-specific questions (their billing, their case status) →
   transfer immediately.
4. Caller asks for a human → transfer.
5. After tool returns, Preeti integrates the result naturally — never
   reads the JSON aloud.

### 8.4 Console output you'll see

```
# Confident answer from KB
🛠️ tool.kb.search   q="What is the document condenser?" k=5
ℹ️  kb.search.done   k=5 top_score=0.812 latency=210ms confident=True
╭─ 🗄️ KB SEARCH ─────────────────────────╮
│ Query     What is the document condenser?│
│ Results   5                             │
│ Top score 0.812                         │
│ Confident True                          │
│ Latency   210ms                         │
╰─────────────────────────────────────────╯

# Low score → transfer
🛠️ tool.kb.search   q="why was my last invoice charged twice?" k=5
ℹ️  kb.search.done   k=5 top_score=0.34  latency=180ms confident=False
╭─ 🚨 TRANSFER TO HUMAN ──────────────────╮
│ Call SID  CAxxxxxxx                     │
│ Admin     +917885820020                 │
│ Reason    account_issue                 │
╰─────────────────────────────────────────╯
ℹ️  twilio.hangup.twiml  replaced TwiML on CAxxxxxxx (will play farewell + bridge)
```

### 8.5 Pre-flight checks before the first KB-grounded call

1. ✅ The admin app's migration has run on `Calling_agent_DB` (so
   `voice_agents`, `kb_documents`, `kb_chunks`, `kb_search_logs`,
   `voice_debug_events` exist + `vector` extension is enabled).
2. ✅ At least one document is uploaded **and** reached `status='ready'`
   for the `preeti` agent (or as a global doc with `agent_id IS NULL`).
3. ✅ `SUPPORT_ADMIN_PHONE` is on the **Twilio Verified Caller IDs** list
   (trial accounts only).
4. ✅ `gemini-embedding-001` is reachable with your `GOOGLE_API_KEY` —
   the admin pipeline succeeding is sufficient proof.

Sanity check the search path without picking up the phone:

```bash
.venv/bin/python -c "
import asyncio
from app.db.database import session_scope
from app.services.kb_search import KbSearchService

async def main():
    async with session_scope() as s:
        r = await KbSearchService(s).search(query='What is Jurinex?')
        print('confident=', r['confident'], 'top_score=', r.get('top_score'))
        for hit in r['results'][:3]:
            print(' -', hit['document_title'], '|', hit.get('heading_path'),
                  '| score=', hit['score'])

asyncio.run(main())
"
```

If that prints chunks with scores `> 0.6` for *"What is Jurinex?"*, you're
ready to call the number and ask Preeti the same question.

### 8.6 What this app will never write

Per the schema contract with the admin app: this app is **read-only**
against `voice_agents`, `kb_documents`, `kb_chunks`, and **never** writes
to `voice_debug_events`. The only KB table it inserts into is
`kb_search_logs` (with `source='voice_agent'`).

## 9. Twilio setup

See [`docs/TWILIO_SETUP.md`](docs/TWILIO_SETUP.md). For local development:

```bash
ngrok http 8000
# put PUBLIC_BASE_URL=https://<id>.ngrok-free.app in .env
# Twilio Console → Phone Numbers → A call comes in → Webhook (POST):
#   https://<id>.ngrok-free.app/twilio/incoming-call
```

## 10. Gemini setup

See [`docs/GEMINI_SETUP.md`](docs/GEMINI_SETUP.md).

## 11. Cloud SQL setup

See [`docs/CLOUD_SQL_SETUP.md`](docs/CLOUD_SQL_SETUP.md).

## 12. How to test an inbound call

1. `docker compose up -d` (or run locally with the steps above).
2. `ngrok http 8000` and update `PUBLIC_BASE_URL` in `.env`; restart the app.
3. In Twilio Console, set the inbound webhook to
   `https://<ngrok>.ngrok-free.app/twilio/incoming-call`.
4. Dial **+18159348556** (or whichever number is configured).
5. Watch `docker compose logs -f app` (or stdout) — Rich panels should fire:

```
📞 CALL STARTED   …
🔌 Twilio media stream started
🤖 GEMINI SESSION STARTED
🛠️ tool.dispatch  create_support_ticket
🎫 TICKET CREATED  JX-20260427-0001
🏁 CALL ENDED
```

## 13. How to test an outbound call

```bash
curl -X POST http://localhost:8000/admin/outbound-call \
  -H "Content-Type: application/json" \
  -H "X-Admin-API-Key: jurinex_admin_demo_key" \
  -d '{
    "to_phone_number": "+919226408823",
    "customer_name": "Demo User",
    "language_hint": "Hindi",
    "reason": "Demo support call"
  }'
```

In `DEMO_MODE` without real Twilio credentials, the response is `queued-demo`
and a row is still created in `calls`.

## 14. How to test in DEMO mode (no Twilio, no Gemini key)

```bash
curl -X POST http://localhost:8000/debug/simulate-conversation \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+919226408823",
    "messages": [
      "Hindi",
      "मुझे OTP नहीं मिल रहा है",
      "हाँ, कृपया ticket बना दीजिए"
    ]
  }'
```

You'll get back a transcript and (typically) `ticket_created: true` with a
`JX-YYYYMMDD-NNNN` number.

## 15. View logs

```bash
docker compose logs -f app
# or, when running locally, watch the uvicorn stdout
```

Logs are Rich-formatted with per-call trace prefixes. Stage names are listed
in [`docs/DATAFLOW.md`](docs/DATAFLOW.md).

## 16. Common errors

See [`docs/DEBUGGING.md`](docs/DEBUGGING.md).

## 17. Next production steps

- Wire real Twilio μ-law(8k) ↔ Gemini PCM16(16k) bridging in
  `app/realtime/audio_codec.py` + `gemini_live_client.py`.
- Replace the deterministic simulator in `GeminiLiveClient._simulate_response`
  with the real live-session loop.
- Replace `app/tools/case_tools.py` stubs with the real Jurinex case API.
- Add Twilio request signature validation on webhooks.
- Add tracing (OpenTelemetry) on top of the existing `trace_context` plumbing.
- Move `metrics` to a real backend (Prometheus / GCP Monitoring).

---

## File tree

```
Jurinex_call_agent/
├── app/
│   ├── api/        # health, twilio, admin, debug
│   ├── realtime/   # gemini_live_client, twilio_media_stream, audio_codec, call_recorder, session_manager, events
│   ├── prompts/    # jurinex_preeti_prompt
│   ├── tools/      # ticket / customer / case / escalation / call / kb / transfer
│   ├── db/         # database, models, schemas, repositories
│   ├── services/   # call, transcript, ticket, summary, compliance, tool_dispatcher, kb_search, gcs_uploader
│   ├── observability/  # logger, rich_console, trace_context, metrics
│   ├── utils/      # security, phone, time_utils
│   ├── config.py
│   ├── lifecycle.py
│   └── main.py
├── alembic/  (env.py + versions/20260427_0001_initial.py)
├── scripts/  (init_db.py, seed_demo_data.py, local_tunnel_notes.md)
├── tests/    (health, phone, ticket, repositories)
├── docs/     (ARCHITECTURE, DATAFLOW, TWILIO, GEMINI, CLOUD_SQL, DEBUGGING)
├── Dockerfile, docker-compose.yml, alembic.ini, Makefile
└── .env.example, .gitignore, requirements.txt, README.md
```

## What is implemented fully

- FastAPI app + lifecycle + health/config/db endpoints
- All DB models + Alembic initial migration + repositories
- Pydantic v2 request/response schemas
- Tool layer:
  - `create_support_ticket`, `lookup_customer`, `check_case_status`
  - `escalate_to_human`, `end_call`
  - **`search_knowledge_base`** — RAG against the admin-owned `kb_chunks`
    table (gemini-embedding-001, 768-d cosine, pgvector ANN)
  - **`transfer_to_human_agent`** — replaces the active TwiML with a Twilio
    `<Dial>` bridge to `SUPPORT_ADMIN_PHONE` (default +91 78858 20020)
- Tool declarations advertised to Gemini Live via `LiveConnectConfig.tools`
- Tool dispatcher with audit trail in `agent_tool_events`
- Twilio webhook handlers + media-stream WebSocket bridge with full
  μ-law/8k ↔ PCM/16k & PCM/24k resampling
- Real Gemini Live audio + transcription enabled
  (`input_audio_transcription` + `output_audio_transcription`)
- Outbound call placement via Twilio REST
- Per-call timeline-mixed `recording.wav` uploaded to GCS
- Auto-hangup safety net: silence timeout, max duration, Gemini-failure
  fallback (all `.env`-tunable)
- Admin REST endpoints (calls, tickets, debug events) protected by API key
- Rich-formatted lifecycle panels + structured `log_dataflow` stages with
  auto-persistence to `call_debug_events` for important stages
- Trace contextvars (session_id / call_sid / direction / customer_phone)
- Demo conversation simulator end-to-end
- Tests for health, phone normalization, ticket-number pattern, DB ping
- Dockerfile + docker-compose + Alembic + Makefile

## What has TODO placeholders

- Real Jurinex case-status API (`tools/case_tools.py` returns deterministic
  fakes)
- Twilio request signature validation
- Cross-call memory (lookup repeat callers + inject last summary into
  the system prompt at session open)










<!-- SILENCE_TIMEOUT_SECONDS=30
MAX_CALL_DURATION_SECONDS=600
AUTO_HANGUP_ON_GEMINI_FAILURE=true
FAREWELL_GRACE_SECONDS=3
TECHNICAL_FAILURE_MESSAGE=We are experiencing a technical issue. Please call back in a few minutes. Goodbye.
Var	Effect
SILENCE_TIMEOUT_SECONDS	After this many seconds with no detected caller speech (RMS < 500), Preeti is asked to politely say goodbye and the line is dropped.
MAX_CALL_DURATION_SECONDS	Hard cap on call length. When hit, Preeti asks the caller to call back, then the line drops.
AUTO_HANGUP_ON_GEMINI_FAILURE	If true, when Gemini's session dies the call is replaced with a fallback <Say> + <Hangup/>. If false, only the panel logs and the line stays open until the caller hangs up.
FAREWELL_GRACE_SECONDS	How long to wait after asking Preeti to say goodbye before actually disconnecting.
TECHNICAL_FAILURE_MESSAGE	The English line spoken when Gemini fails (no live agent available to localize). -->








curl -X POST http://localhost:8000/admin/outbound-call   -H "Content-Type: application/json"   -H "X-Admin-API-Key: jurinex_admin_demo_key"   -d '{"to_phone_number":"+917875827092","customer_name":"Demo User","language_hint":"Hindi","reason":"Demo"}'