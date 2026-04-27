# Local Tunnel Notes (ngrok)

To expose the local FastAPI server to Twilio:

```bash
ngrok http 8000
```

Copy the `https://...ngrok-free.app` URL and:

1. Set `PUBLIC_BASE_URL=https://<id>.ngrok-free.app` in `.env`
2. Restart the app
3. In the Twilio Console → Phone Numbers → choose your number:
   - Voice: A call comes in → Webhook → `POST` →
     `https://<id>.ngrok-free.app/twilio/incoming-call`
   - Status callback (optional) → `https://<id>.ngrok-free.app/twilio/call-status`

Twilio will then connect each call to our `wss://.../twilio/media-stream`.
