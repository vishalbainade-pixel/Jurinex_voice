# Architecture

## Layered overview

```
┌──────────────────────────────────────────────────────────────────┐
│                       FastAPI (app/main.py)                      │
│                                                                  │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐              │
│  │ health  │  │ twilio  │  │ admin   │  │ debug   │   API layer  │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘              │
│       │            │            │            │                   │
│       │            ▼            ▼            ▼                   │
│       │   ┌─────────────────────────────────────┐                │
│       │   │            services/                │   business     │
│       │   │  CallService, TranscriptService,    │   logic        │
│       │   │  TicketService, SummaryService,     │                │
│       │   │  ComplianceService, tool_dispatcher │                │
│       │   └────────────────┬────────────────────┘                │
│       │                    │                                     │
│       │   ┌────────────────▼─────────────────┐                   │
│       │   │             db/                  │   persistence    │
│       │   │  models, repositories, schemas   │                   │
│       │   └────────────────┬─────────────────┘                   │
│       ▼                    ▼                                     │
│   ┌────────────────────────────────────────────┐                 │
│   │   realtime/  (Twilio ↔ Gemini bridge)      │   realtime     │
│   │  TwilioMediaStreamHandler, SessionManager, │                 │
│   │  GeminiLiveClient, audio_codec, events     │                 │
│   └────────────────────────────────────────────┘                 │
└──────────────────────────────────────────────────────────────────┘
                ▲                                  ▲
                │                                  │
        Twilio Media Streams                  Gemini Live API
        (WebSocket μ-law/8k)                 (google-genai SDK)
```

## Responsibilities

- **api/** — thin HTTP handlers, validation, dependency injection
- **services/** — orchestrate repositories + tools, no SQL strings
- **db/** — SQLAlchemy 2.x async models, repositories, Pydantic schemas
- **realtime/** — Twilio WebSocket handler + Gemini client + session registry
- **tools/** — agent-callable tools (ticket, customer, escalation, case, end-call)
- **observability/** — Rich console panels, structured logger, contextvars trace
- **utils/** — phone normalization, time helpers, security guards

## Concurrency model

Each Twilio call gets:
1. A `CallSession` in `SessionManager` (in-memory, keyed by session_id).
2. A `GeminiLiveClient` instance with its own asyncio queue.
3. A background task `_consume_gemini_events` that drains Gemini events and
   pushes audio/text back to Twilio.

The DB session is **not** held across the full call — `session_scope()` is
used per-event, which keeps connections short-lived.
