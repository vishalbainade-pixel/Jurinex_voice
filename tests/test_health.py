from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_config_redacts_secrets():
    client = TestClient(app)
    resp = client.get("/health/config")
    assert resp.status_code == 200
    body = resp.json()
    # Sanity: never expose tokens / keys.
    for forbidden in ("twilio_auth_token", "secret_key", "gemini_api_key", "admin_api_key"):
        assert forbidden not in body
