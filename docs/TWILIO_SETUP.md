# Twilio Setup

## 1. Trial account constraints

You're on a Twilio trial. Two important limits:

- You can **only call verified caller IDs**. Add `+919226408823` (or whichever
  destination) under *Phone Numbers → Verified Caller IDs*.
- A trial-account preamble plays before the agent's audio. That's expected.

## 2. Inbound webhook

Twilio Console → Phone Numbers → `+18159348556`:

- **A call comes in** → Webhook → `POST` →
  `https://<PUBLIC_BASE_URL>/twilio/incoming-call`
- **Status callback** (optional) →
  `https://<PUBLIC_BASE_URL>/twilio/call-status`

Replace `<PUBLIC_BASE_URL>` with your ngrok or Cloud Run URL (no trailing slash).

## 3. Local tunnel (development)

```bash
ngrok http 8000
```

Set `PUBLIC_BASE_URL=https://<id>.ngrok-free.app` in `.env` and restart.

## 4. Outbound test (after deploying)

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

## 5. Common errors

| Error | Cause | Fix |
| --- | --- | --- |
| 21210 | "From" number not owned by account | Set `TWILIO_PHONE_NUMBER` to a Twilio-owned number |
| 21215 | International permission off | Enable the destination region in Geo Permissions |
| 13224 | Number not verified (trial) | Add destination under Verified Caller IDs |
| 11200 | HTTP retrieval failure | TwiML URL not reachable — check `PUBLIC_BASE_URL` and ngrok |
