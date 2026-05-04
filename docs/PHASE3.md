# Phase 3 — Admin Coverage Gaps

Tracks the remaining items beyond Phase 1 (DB-driven config + prompt) and
Phase 2 (calendar tools). Each item has its own section that gets filled in
as the work lands. The format for every item is identical:

  * **Status** — pending / in-progress / done
  * **What changed** — files touched + key behaviour
  * **Logs** — every dataflow / debug / error log key emitted by the change
  * **Smoke test** — exact command + observed output

Reading this document top-to-bottom should be enough to understand what the
bridge does today and where it differs from before Phase 3.

---

## Table of contents

1. [agent_transfer tool](#1-agent_transfer-tool) — hand the call off to a different voice agent
2. [Post-call extraction job](#2-post-call-extraction-job) — populate `voice_post_call_extractions` + `voice_call_enrichments`
3. [voice_debug_events writer](#3-voice_debug_events-writer) — pipeline trace events
4. [Live session resilience — auto-resume](#4-live-session-resilience--auto-resume) — reconnect on WS drop
5. [Multi-agent routing](#5-multi-agent-routing) — pick `agent_name` per call
6. [Admin reload endpoint](#6-admin-reload-endpoint) — invalidate prompt cache
7. [voice_model_pricing lookup](#7-voice_model_pricing-lookup) — cost-stamp each call
8. [platform_voices catalogue validation](#8-platform_voices-catalogue-validation) — fail loud on bad voice
9. [Calendar DWD note](#9-calendar-dwd-note) — when to flip `JURINEX_VOICE_CALENDAR_ALLOW_ATTENDEES`

---

## 1. agent_transfer tool

> **Status:** ✅ done

### What changed

* `app/db/schemas.py` — `AgentTransferInput(target_agent_name?, target_agent_id?, reason, handoff_message?, language)`.
* `app/db/voice_agent_repository.py` — new `load_active_bundle_by_id(id)` and `list_active_agents()`. The big SELECT was extracted into `_SELECT_COLUMNS` so the by-name and by-id queries share one column list.
* `app/tools/agent_transfer_tools.py` — new handler. Resolves target by id then by name, refuses noop transfers (target == current agent), refuses unknown targets and lists the active alternatives.
* `app/services/tool_dispatcher.py` — new `agent_transfer` branch.
* `app/realtime/gemini_live_client.py` — new `FunctionDeclaration` for `agent_transfer`.
* `app/realtime/twilio_media_stream.py` — new `_swap_agent(...)` method. When the dispatcher returns `action='swap_agent'`, the bridge sends the tool response to the OLD model first (so its turn closes cleanly), closes the OLD `GeminiLiveClient`, opens a NEW one with the target bundle's `live_model` / `voice_name` / `temperature` / enabled tools, replaces `self.bundle` + `self.session.gemini` + restarts `_consume_gemini_events()`, then primes the new agent with the handoff message so it speaks first. **Twilio Media Stream stays open** — caller hears no hang-up.

### Logs emitted

| key | level | when |
|---|---|---|
| `tool.agent_transfer.resolved` | info | target bundle loaded successfully |
| `tool.agent_transfer.not_found` | warning | target agent name/id is missing or inactive |
| `tool.agent_transfer.noop` | warning | target == current agent |
| `agent.swap.target_loaded` | info | bridge resolved the new bundle and built its system instruction |
| `agent.swap.old_close_error` | warning | old session close raised (non-fatal) |
| `agent.swap.new_connect_failed` | error | new Gemini connect failed — bridge stays on the old agent |
| `agent.swap.complete` | info | hot-swap succeeded |

### Smoke test

```
.venv/bin/python -c "
import asyncio
from app.db.database import session_scope
from app.db.voice_agent_repository import VoiceAgentRepository
from app.db.schemas import AgentTransferInput
from app.tools.agent_transfer_tools import agent_transfer
async def main():
    async with session_scope() as s:
        b = await VoiceAgentRepository(s).load_active_bundle('preeti')
        # noop, unknown, by-id, list
        ...
asyncio.run(main())"
```

Observed:

```
TEST 1: noop transfer (preeti → preeti)
  result: {'success': False, 'message': "already on agent 'preeti' — pick a different one"}
TEST 2: nonexistent target
  → tool.agent_transfer.not_found requested name='not_a_real_agent'
  success: False | no active voice agent matches name='not_a_real_agent'
TEST 3: load by id (no current bundle)
  → AGENT TRANSFER panel printed; tool.agent_transfer.resolved
  success: True | target_agent_name: preeti
TEST 4: list_active_agents → ['preeti']
```

## 2. Post-call extraction job

> **Status:** ✅ done

### What changed

* `app/db/voice_post_call_repository.py` — `VoicePostCallExtractionsRepository` (insert running → mark completed/failed) + `VoiceCallEnrichmentsRepository` (UPSERT keyed on `call_id`).
* `app/services/post_call_service.py` — pulls enabled fields from `bundle.agent_builder.post_call_extraction`, picks `bundle.agent_builder.post_call_model` (default `gemini-2.5-flash`), assembles the transcript from `call_messages`, calls Gemini in a worker thread with a JSON-only prompt, coerces / zero-fills the response to match the declared schema, then writes BOTH tables.
* `app/realtime/twilio_media_stream.py` `_teardown` — invokes `run_post_call_extraction(...)` after `mark_completed` + summary + recording upload. Wrapped in try/except so a failed extraction never blocks teardown.

### Logs emitted

| key | level | when |
|---|---|---|
| `post_call.extraction.started` | info | row inserted with status=running |
| `post_call.extraction.completed` | info | model returned + row updated |
| `post_call.extraction.failed` | warning | model errored — row marked failed, zero-filled extracted_data |
| `post_call.empty_transcript` | warning | call_messages empty — model skipped, schema-shaped zero-fill written |
| `post_call.enrichment.upserted` | info | voice_call_enrichments upsert done |
| `post_call.done` | info | end of extraction pipeline |
| `POST-CALL EXTRACTION FAILED` (panel) | error | unhandled error inside extraction body |
| `POST-CALL HOOK FAILED` (panel) | error | unhandled error in the bridge teardown wrapper |

### Smoke test

Synthetic call (`SMOKE_TEST_…`) with a 5-turn transcript was inserted, then `run_post_call_extraction(...)` was invoked.

```
POST-CALL EXTRACTION  Call=44bad862…  Model=gemini-2.5-flash  Fields=call_summary,call_successful,user_sentiment,preferred_language  Transcript chars=278
post_call.extraction.started  id=3d1a8129…  fields=['call_summary','call_successful','user_sentiment','preferred_language']
post_call.extraction.completed  id=3d1a8129…  latency_ms=4701
post_call.enrichment.upserted   call=44bad862…  outcome=successful  successful=True  language=English
extracted: {
  'call_summary': 'The customer inquired about Jurinex, and the agent provided a definition for the platform.',
  'call_successful': True,
  'user_sentiment': 'positive',
  'preferred_language': 'English'
}
voice_call_enrichments row: successful=True, preferred_language='English', end_reason='caller_hangup', recording_url='gs://bucket/x.wav'
```

Round-trip verified end-to-end against live Gemini and live DB; rows cleaned up after.

## 3. voice_debug_events writer

> **Status:** ✅ done

### What changed

* `app/db/voice_debug_events_repository.py` — `VoiceDebugEventsRepository.emit(event_type, event_stage, message, payload, trace_id, agent_id)`. JSONB payload is encoded with `default=str` so UUIDs / datetimes survive.
* `app/realtime/twilio_media_stream.py` — new `_debug_event(...)` helper that schedules a fire-and-forget `asyncio.create_task` write so the realtime path is never blocked. Hooked into:
  * `_on_start` after the `CALL STARTED` panel — emits `bridge / call.started` with agent + model + voice + tool list + greeting flag.
  * `_swap_agent` after a successful hot-swap — emits `bridge / agent.swap`.
  * `_graceful_hangup` — emits `watchdog / auto_hangup`.
  * `_teardown` after the `CALL ENDED` panel — emits `bridge / call.ended` with the final terminate_reason and recording uris.

### Logs emitted

| key | level | when |
|---|---|---|
| `debug_event.persisted` | debug | every successful row insert |
| `debug_event.write_error` | warning | the fire-and-forget task hit an exception (rare; row is dropped) |

### Smoke test

```
.venv/bin/python -c "VoiceDebugEventsRepository(s).emit(event_type='bridge', event_stage='call.started', message='smoke-test row', trace_id=..., payload={'foo':'bar','count':3})"
```

Result:

```
DEBUG  debug_event.persisted type=bridge stage=call.started msg=smoke-test row
row: {'event_type': 'bridge', 'event_stage': 'call.started', 'message': 'smoke-test row', 'payload': {'foo': 'bar', 'count': 3}}
```

Round-trip verified; row cleaned up after.

## 4. Live session resilience — auto-resume

> **Status:** ✅ done

### What changed

* `app/realtime/gemini_live_client.py`
  * New instance attribute `_resume_count` (init to 0).
  * New `async _attempt_resume(reason)`: cancels the dead receive task, exits the dead context manager, calls `_open_real_session(...)` again with the same `_session_id` + `_system_prompt`. Caps at `settings.jurinex_voice_live_max_resumes`.
  * `_disable_session(...)` now schedules a task that first calls `_attempt_resume(...)` and only flips `_send_disabled = True` + fires `on_session_dead` when the resume budget is exhausted (or the loop is gone).

### Logs emitted

| key | level | when |
|---|---|---|
| `GEMINI RESUME` (panel) | warning | resume attempt N of MAX kicked off |
| `gemini.resume.ok` | info | resume succeeded |
| `gemini.resume.failed` | error | one attempt failed (will try again until budget hits) |
| `gemini.resume.exhausted` | warning | budget exhausted — falling through to `GEMINI SESSION DEAD` |
| `GEMINI SESSION DEAD` (panel) | error | now only fires AFTER the resume budget is spent |

### Smoke test

```
.venv/bin/python -c "
from app.realtime.gemini_live_client import GeminiLiveClient
c = GeminiLiveClient()
# (1) resume without a recorded prompt → should refuse instantly
# (2) resume with budget == max → should log gemini.resume.exhausted
"
```

Result:

```
no-prompt resume returned: False (expected False)
WARNING gemini.resume.exhausted resumes=3 limit=3 — giving up
budget-exhausted resume returned: False (expected False)
max_resumes config = 3
```

## 5. Multi-agent routing

> **Status:** ✅ done

### What changed

* `app/realtime/twilio_media_stream.py` `_on_start` now picks the agent for THIS call from `customParameters.agent_name` (set by Twilio per phone number / Studio flow), falling back to `settings.kb_agent_name` if not present.

### Logs emitted

| key | level | when |
|---|---|---|
| `agent.routing.selected` | info | logs `requested=<name>` and `source=twilio_param` or `env` |
| `agent.bundle.not_found` | warning | (existing) — fires when the requested name has no DB row, the bridge then falls back to the static prompt |

### Operator notes

To route different numbers to different agents, set the `agent_name`
custom parameter in your TwiML:

```xml
<Response>
  <Connect>
    <Stream url="wss://your-ngrok/twilio/media">
      <Parameter name="agent_name" value="rohit_sales"/>
    </Stream>
  </Connect>
</Response>
```

In Twilio Studio, add a Stream widget with the same `agent_name` parameter
keyed off the inbound number.

### Smoke test

```
twilio param wins : rohit_sales
env fallback      : preeti
whitespace ignored: preeti
```

## 6. Admin reload endpoint

> **Status:** ✅ done

### What changed

* `app/api/admin_routes.py` — new `POST /admin/voice/reload` (already gated by `require_admin_api_key`). Calls `PromptFragmentsRepository.invalidate_cache()`.

### Logs emitted

| key | level | when |
|---|---|---|
| `prompts.cache.invalidated` | info | invalidator ran |
| `admin.voice.reload` | info | the endpoint was hit |

### Smoke test

```
miss → hit → invalidate → miss
```

```
INFO  prompts.cache.miss fragments — refreshing from DB
DEBUG prompts.cache.hit fragments age_ms=2
--- INVALIDATING ---
INFO  prompts.cache.invalidated fragments + tool_prompts
INFO  prompts.cache.miss fragments — refreshing from DB
```

### Operator note

```
curl -X POST -H "x-admin-api-key: $ADMIN_API_KEY" \
     https://your-bridge/admin/voice/reload
```

## 7. voice_model_pricing lookup

> **Status:** ✅ done

### What changed

* `app/services/pricing_service.py` — `VoiceModelPricingRepository.lookup(model_id)` + dataclass `ModelPricing` with `cost_usd(caller_seconds, agent_seconds)`. `compute_call_cost(...)` is the convenience wrapper used by the bridge.
* `app/db/voice_post_call_repository.py` — `VoiceCallEnrichmentsRepository.upsert(...)` now accepts `cost_usd` and writes it to `voice_call_enrichments.cost_usd` (kept on conflict via `COALESCE`).
* `app/services/post_call_service.py` — accepts `pricing_payload`, threads `cost_usd` into the upsert, and stuffs the full pricing block under `analysis.pricing` so the dashboard sees both rates and totals.
* `app/realtime/twilio_media_stream.py` `_teardown` — calls `compute_call_cost(...)` using the recorder's caller/agent seconds and the agent bundle's `live_model`, stamps `calls.raw_metadata.pricing`, and forwards the payload into `run_post_call_extraction(...)`.

### Logs emitted

| key | level | when |
|---|---|---|
| `pricing.computed` | info | cost computed (model + minutes + USD + INR) |
| `pricing.lookup.miss` | warning | model_id not in voice_model_pricing |
| `pricing.error` | warning | generic exception during compute (non-fatal) |

### Smoke test

```
gemini-3.1-flash-live-preview cost (42.5s caller / 37s agent):
  caller_seconds: 42.5
  agent_seconds: 37.0
  total_minutes: 1.325
  input_audio_usd_per_minute: 0.005
  output_audio_usd_per_minute: 0.018
  cost_usd: 0.014642
  cost_inr_estimate: 2.544

gemini-2.5-flash (no audio rates): cost_usd: None  cost_inr_estimate: 0.0
unknown model: pricing.lookup.miss → returns None
```

Math sanity:

```
caller min  = 42.5 / 60 = 0.7083  ⇒  0.7083 × 0.005 = 0.003542
agent min   = 37.0 / 60 = 0.6167  ⇒  0.6167 × 0.018 = 0.011100
                                     total           = 0.014642 USD ✓
```

## 8. platform_voices catalogue validation

> **Status:** ✅ done

### What changed

* `app/db/platform_voices_repository.py` — `PlatformVoicesRepository.active_voices()` returns a `dict[voice_name → PlatformVoice]` for active Gemini voices. Process-cached for 5 minutes.
* `app/realtime/twilio_media_stream.py` `_on_start` — after resolving `voice_name`, validates against the catalogue. On miss: logs at ERROR with the catalogue size + a sample of valid voices, then falls back to `Aoede` so the call still completes. Catalogue lookup failure is non-fatal (warning + proceed).

### Logs emitted

| key | level | when |
|---|---|---|
| `platform_voices.cache.miss` / `.hit` | info / debug | catalogue load |
| `platform_voices.loaded` | info | first load — count + sample |
| `voice.validation.ok` | debug | requested voice is in catalogue |
| `voice.validation.failed` | error | requested voice missing — fallback applied, sample shown |
| `voice.validation.error` | warning | catalogue read raised — proceed unvalidated |

### Smoke test

```
platform_voices.loaded count=23 sample=['Kore','Achernar','Schedar','Callirrhoe','Despina']
Achernar (preeti):   True
Leda (legacy env):   True
Aoede (fallback):    True
TotallyMadeUp:       False
second call (cache): True
```

## 9. Calendar DWD note

> **Status:** ✅ done (one-line `.env` flip when DWD is enabled)

### Background

Phase 2 wired `calendar_book` to honour `JURINEX_VOICE_CALENDAR_ALLOW_ATTENDEES`:

* `false` (current default) — events are created on the configured calendar but **no invite emails go out**. Google's API call returns 200 OK; calendar service accounts can write to the calendar without DWD.
* `true` — `app/services/google_calendar.py` `insert_event(...)` includes the `attendees=[…]` array and sets `sendUpdates=all`. Without Domain-Wide Delegation on the service account, Google rejects this with `HTTP 403 — Service accounts cannot invite attendees without Domain-Wide Delegation of Authority`.

### Operator steps to enable invites

1. **In Google Workspace Admin Console** → Security → Access and data control → API controls → Manage Domain Wide Delegation → Add new.
2. Client ID = the SA's `client_id` (already in `JURINEX_VOICE_CALENDAR_SA_JSON_BASE64`).
3. OAuth scopes = `https://www.googleapis.com/auth/calendar`.
4. **In `.env`** flip:

   ```dotenv
   JURINEX_VOICE_CALENDAR_ALLOW_ATTENDEES=true
   ```

5. Restart the bridge (or wait for the next deploy). No code change needed.

### Verifying

After the flip, `calendar_book` results will include `attendees_emailed: true` in the tool response. The created event row in `voice_calendar_bookings.metadata` will also show `attendees_emailed: true`.

### What happens if you flip without DWD

The `insert_event` call raises:

```
calendar API POST /calendars/.../events → HTTP 403:
  Service accounts cannot invite attendees without Domain-Wide Delegation of Authority.
```

`calendar_book` writes a `voice_calendar_bookings` row with `status='failed'` + the error in `metadata.error`, and returns `success=false` to the model. Caller doesn't get an event. Flip back to `false` to restore booking.
