"""Centralized application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings backed by environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = Field(default="Jurinex_call_agent")
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    log_level: str = "DEBUG"
    demo_mode: bool = True

    public_base_url: str = "http://localhost:8000"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Gemini
    gemini_api_key: str = ""
    google_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-live-preview"
    gemini_voice: str = "Aoede"

    # Database
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/jurinex_call_agent"
    )
    sync_database_url: str = (
        "postgresql+psycopg2://postgres:postgres@localhost:5432/jurinex_call_agent"
    )

    # Cloud SQL
    cloud_sql_connection_name: str = ""
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "jurinex_call_agent"

    # Auth
    secret_key: str = "change_this_secret"
    admin_api_key: str = "change_me"

    # Call lifecycle controls (all env-overridable)
    silence_timeout_seconds: int = 30           # A — caller silence → hang up
    max_call_duration_seconds: int = 600        # B — hard cap on a call (10 min)
    auto_hangup_on_gemini_failure: bool = True  # D — drop the line if Gemini dies
    farewell_grace_seconds: int = 3             # how long to let Preeti's goodbye play
    technical_failure_message: str = (
        "We are experiencing a technical issue. Please call back in a few minutes. Goodbye."
    )

    # Knowledge base (RAG) — read against the shared admin-owned KB tables
    kb_enabled: bool = True
    kb_agent_name: str = "preeti"        # voice_agents.name to scope our queries
    kb_embedding_model: str = "gemini-embedding-001"
    kb_embedding_dim: int = 768
    kb_search_k: int = 5
    kb_min_score: float = 0.60           # below this Preeti must hand off
    # Shadow-RAG (proactive context injection on every caller turn) uses a
    # LOWER threshold because the caller's raw transcript is in Hindi/Marathi
    # while docs are indexed in English — cross-language cosine is naturally
    # weaker than same-language. Set to 0 to inject the top hit always; raise
    # to disable shadow injection entirely.
    kb_shadow_enabled: bool = True
    kb_shadow_min_score: float = 0.50

    # Eager greeting — Twilio plays a pre-rendered Hindi greeting via <Say>
    # *before* the media stream opens, so the caller hears something within
    # ~500 ms of pickup (the Gemini Live cold-start would otherwise leave
    # 4-6 s of dead air). By the time the static greeting finishes, the
    # Gemini session is open and Preeti can respond to the caller's reply.
    eager_greeting_enabled: bool = True
    eager_greeting_text: str = (
        "नमस्ते, Jurinex support से संपर्क करने के लिए धन्यवाद। "
        "मैं Preeti बोल रही हूँ। मैं आपकी मदद English, Hindi या Marathi में "
        "कर सकती हूँ। आप कौन सी भाषा पसंद करेंगे?"
    )
    eager_greeting_voice: str = "Google.hi-IN-Neural2-A"
    eager_greeting_language: str = "hi-IN"
    # If set, Twilio plays this audio file (WAV/MP3) instead of TTS-reading
    # `eager_greeting_text`. Lets you ship Preeti's exact voice. Accepts
    # either a full URL (https://…) or a path relative to the app
    # (e.g. "/static/greeting.wav") which gets prefixed with PUBLIC_BASE_URL.
    eager_greeting_audio_url: str = ""

    # Human-agent transfer (Twilio Dial bridge)
    support_admin_phone: str = "+917885820020"
    # How long Twilio rings the admin before falling back to a polite goodbye.
    transfer_dial_timeout_seconds: int = 30

    # On-hold message Twilio plays to the caller while the admin's phone rings.
    # We keep three language variants and pick one based on the caller's
    # selected language at transfer time. Each ~12-15 seconds of speech.
    transfer_hold_message_en: str = (
        "Please stay on the line, I am connecting you to our Jurinex support team. "
        "While we connect you, here is a quick overview of Jurinex. "
        "Jurinex is an AI legal intelligence platform built for Indian lawyers. "
        "It includes a Smart Case Summarizer that condenses lengthy judgements in seconds, "
        "AI Case Creation that drafts plaints, writs and bail applications from basic facts, "
        "a real-time Citation Service powered by India Kanoon, "
        "and an AI Document Drafter for sale deeds, agreements, notices and more. "
        "A support specialist will be with you shortly. Thank you for your patience."
    )
    transfer_hold_message_hi: str = (
        "कृपया लाइन पर बने रहें, मैं आपको हमारी Jurinex support team से जोड़ रही हूँ। "
        "जब तक हम आपको जोड़ते हैं, आइए Jurinex के बारे में एक संक्षिप्त परिचय देते हैं। "
        "Jurinex भारतीय वकीलों के लिए बनाया गया एक AI Legal Intelligence Platform है। "
        "इसमें Smart Case Summarizer है जो लंबे judgements को सेकंडों में सारांश में बदल देता है, "
        "AI Case Creation है जो basic facts से plaints, writ petitions और bail applications तैयार करता है, "
        "India Kanoon से जुड़ी real-time Citation Service है, "
        "और AI Document Drafter है जो sale deeds, agreements, notices और बहुत कुछ बनाता है। "
        "एक support specialist बस कुछ ही पलों में आपसे बात करेंगे। आपके धैर्य के लिए धन्यवाद।"
    )
    transfer_hold_message_mr: str = (
        "कृपया लाइनवर थांबा, मी तुम्हाला आमच्या Jurinex support team कडे जोडत आहे. "
        "जोपर्यंत आम्ही तुम्हाला जोडतो, तोपर्यंत Jurinex बद्दल थोडक्यात माहिती ऐका. "
        "Jurinex हे भारतीय वकीलांसाठी तयार केलेले AI Legal Intelligence Platform आहे. "
        "यात Smart Case Summarizer आहे जो लांब judgements काही सेकंदात summary मध्ये बदलतो, "
        "AI Case Creation आहे जे basic facts वरून plaints, writ petitions आणि bail applications तयार करते, "
        "India Kanoon शी जोडलेली real-time Citation Service आहे, "
        "आणि AI Document Drafter आहे जो sale deeds, agreements, notices आणि बरेच काही तयार करतो. "
        "एक support specialist काही क्षणांत तुमच्याशी बोलणार आहेत. तुमच्या संयमाबद्दल धन्यवाद."
    )

    # Optional Twilio TTS voice overrides for the hold message (per language).
    # Examples:
    #   Google.hi-IN-Neural2-A     ← natural Indian Hindi female (Neural2)
    #   Google.hi-IN-Wavenet-A     ← Wavenet alternative
    #   Polly.Aditi-Neural         ← Amazon Polly neural Hindi/English
    #   Polly.Joanna-Neural        ← Amazon Polly neural English (US)
    #   alice                      ← Twilio's default basic voice
    # See https://www.twilio.com/docs/voice/twiml/say/text-speech for the full list.
    transfer_hold_voice_en: str = "Google.en-IN-Neural2-A"
    transfer_hold_voice_hi: str = "Google.hi-IN-Neural2-A"
    transfer_hold_voice_mr: str = "Google.mr-IN-Wavenet-A"

    # GCS recordings — buffer caller + agent audio in memory, upload at call end
    gcs_recordings_enabled: bool = True
    gcs_bucket: str = "jurinex-voice"
    google_application_credentials: str = ""    # path to GCP service-account JSON
    # Alternative to a JSON file path: paste a base64-encoded service-account key
    # directly into the env var. Easier for Cloud Run / Docker deployment because
    # no file mount is needed. ``gcs_key_base64`` wins over the file path if both
    # are set.
    gcs_key_base64: str = ""
    # Optional explicit project override. If set, used as the Storage client's
    # billing project; otherwise the SA JSON's `project_id` is used.
    gcs_project_id: str = ""

    @property
    def gemini_key(self) -> str:
        """Return whichever Gemini key is configured."""
        return self.gemini_api_key or self.google_api_key

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()


settings = get_settings()
