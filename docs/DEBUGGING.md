# Debugging

## Where do logs go?

All logs are emitted by `app/observability/logger.py` through Rich. Each line
includes a `[call=… direction=… sid=…]` prefix derived from the contextvars
trace. Important lifecycle events are also rendered as Rich panels.

Set `LOG_LEVEL=DEBUG` for full payload previews.

## Health checks

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/db
curl http://localhost:8000/health/config
```

## Demo conversation (no Twilio, no Gemini key required)

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

## Inspecting state via admin

```bash
H="X-Admin-API-Key: jurinex_admin_demo_key"

curl -H "$H" http://localhost:8000/admin/calls
curl -H "$H" http://localhost:8000/admin/tickets
curl -H "$H" http://localhost:8000/admin/debug-events
```

## Common errors

| Symptom | Likely cause |
| --- | --- |
| `db not reachable` on startup | wrong `DATABASE_URL`, IP not allow-listed, or async driver missing |
| `401 invalid admin api key` | missing `X-Admin-API-Key` header |
| `21210` from Twilio | `TWILIO_PHONE_NUMBER` not owned by the Twilio account |
| WebSocket closes immediately | `PUBLIC_BASE_URL` doesn't match the host Twilio dialed |
| Agent stays silent | Real audio bridge not wired yet — keep `DEMO_MODE=true` until it is |
