# Jurinex_call_agent — Preeti, the multilingual support voice agent

Production-grade Python backend for a multilingual (English / Hindi / Marathi)
AI voice customer-support agent named **Preeti**, for the **Jurinex** platform.

It bridges:

- **Twilio Media Streams** for telephony
- **Gemini Live API** for realtime conversation
- **Cloud SQL PostgreSQL** for transcripts, tickets, escalations, debug events
- **FastAPI + Rich** for the HTTP surface and console observability

> ⚠️ Audio bridging between Twilio's μ-law/8kHz and Gemini's PCM/16kHz is a
> deliberate, well-marked TODO surface. The rest of the system — DB, tools,
> transcripts, dataflow logs, summaries — is fully implemented and runnable in
> `DEMO_MODE=true`.

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
api/  →  services/  →  db/repositories
                   ↘
                    tools/  ←  realtime/  ←  Twilio + Gemini
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

See [`.env.example`](.env.example). Highlights:

| Var | Purpose |
| --- | --- |
| `DATABASE_URL` | async SQLAlchemy URL (asyncpg) |
| `SYNC_DATABASE_URL` | sync URL used by Alembic |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio REST creds |
| `TWILIO_PHONE_NUMBER` | the Twilio number used as the "from" |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Gemini Live key |
| `DEMO_MODE` | `true` short-circuits Gemini with a deterministic simulator |
| `ADMIN_API_KEY` | required `X-Admin-API-Key` header for `/admin/*` |
| `PUBLIC_BASE_URL` | what Twilio sees (your ngrok / Cloud Run URL) |

## 7. Twilio setup

See [`docs/TWILIO_SETUP.md`](docs/TWILIO_SETUP.md). For local development:

```bash
ngrok http 8000
# put PUBLIC_BASE_URL=https://<id>.ngrok-free.app in .env
# Twilio Console → Phone Numbers → A call comes in → Webhook (POST):
#   https://<id>.ngrok-free.app/twilio/incoming-call
```

## 8. Gemini setup

See [`docs/GEMINI_SETUP.md`](docs/GEMINI_SETUP.md).

## 9. Cloud SQL setup

See [`docs/CLOUD_SQL_SETUP.md`](docs/CLOUD_SQL_SETUP.md).

## 10. How to test an inbound call

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

## 11. How to test an outbound call

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

## 12. How to test in DEMO mode (no Twilio, no Gemini key)

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

## 13. View logs

```bash
docker compose logs -f app
# or, when running locally, watch the uvicorn stdout
```

Logs are Rich-formatted with per-call trace prefixes. Stage names are listed
in [`docs/DATAFLOW.md`](docs/DATAFLOW.md).

## 14. Common errors

See [`docs/DEBUGGING.md`](docs/DEBUGGING.md).

## 15. Next production steps

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
│   ├── realtime/   # gemini_live_client, twilio_media_stream, audio_codec, session_manager, events
│   ├── prompts/    # jurinex_preeti_prompt
│   ├── tools/      # ticket / customer / case / escalation / call
│   ├── db/         # database, models, schemas, repositories
│   ├── services/   # call, transcript, ticket, summary, compliance, tool_dispatcher
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
- Tool layer: `create_support_ticket`, `lookup_customer`, `check_case_status`,
  `escalate_to_human`, `end_call`
- Tool dispatcher with audit trail in `agent_tool_events`
- Twilio webhook handlers + media-stream WebSocket bridge
- Gemini Live client wrapper (logical session + simulated replies)
- Outbound call placement via Twilio REST
- Admin REST endpoints (calls, tickets, debug events) protected by API key
- Rich-formatted lifecycle panels + structured `log_dataflow` stages
- Trace contextvars (session_id / call_sid / direction / customer_phone)
- Demo conversation simulator end-to-end
- Tests for health, phone normalization, ticket-number pattern, DB ping
- Dockerfile + docker-compose + Alembic + Makefile

## What has TODO placeholders

- μ-law(8k) ↔ PCM16(16k) audio bridging (clearly marked in `audio_codec.py`)
- Real Gemini Live audio I/O — currently logical session + deterministic
  simulator (clearly marked in `gemini_live_client.py`)
- Real Jurinex case-status API (`tools/case_tools.py` returns deterministic
  fakes)
- Twilio request signature validation
