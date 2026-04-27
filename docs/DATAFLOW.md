# Dataflow

## Inbound call

```
Caller dials +18159348556
        │
        ▼
Twilio → POST /twilio/incoming-call           (twilio.webhook.received)
        │
        ▼
FastAPI returns TwiML <Connect><Stream …/>    (twilio.twiml.generated)
        │
        ▼
Twilio opens WS  /twilio/media-stream         (twilio.websocket.accepted)
        │
        ├─ event=start    → DB Call row + SessionManager + GeminiLiveClient.connect
        ├─ event=media    → audio_codec.decode → gemini.send_audio
        ├─ event=mark     → logged
        └─ event=stop     → teardown
                                 │
                                 ▼
              GeminiLiveClient.receive_events() loop
                                 │
              ├─ text         → TranscriptService.save_message + send mark to Twilio
              ├─ audio        → audio_codec.encode → ws.send(media)
              ├─ tool_call    → tool_dispatcher.dispatch_tool_call(...)
              └─ session_close→ break
                                 │
                                 ▼
                     CallService.mark_completed
                     SummaryService.build_summary
```

## Outbound call

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
Same TwiML <Connect><Stream …/> path as inbound; from here on, identical.
```

## Stage names emitted via `log_dataflow`

```
twilio.webhook.received
twilio.twiml.generated
twilio.websocket.accepted
twilio.media.connected
twilio.media.start
twilio.media.chunk
twilio.media.outbound
twilio.media.stop
twilio.call.status

gemini.session.create
gemini.session.open
gemini.session.close
gemini.audio.input
gemini.text.input
gemini.response.text
gemini.tool_call

db.message.saved
db.ping

tool.dispatch
tool.lookup_customer
tool.ticket.create
tool.escalation.create
tool.case.status
tool.end_call

call.summary.created
```
