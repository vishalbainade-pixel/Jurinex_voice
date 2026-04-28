"""Thin wrapper around google-cloud-storage for call-recording uploads.

The upload itself is synchronous (the GCS Python client is sync), so we run
each upload via ``asyncio.to_thread`` from the caller to avoid blocking the
event loop.

Auth precedence (first match wins):
  1. ``GCS_KEY_BASE64``  — base64 of the service-account JSON pasted into env
  2. ``GOOGLE_APPLICATION_CREDENTIALS`` — path to a JSON file on disk
  3. Application Default Credentials (Cloud Run / GKE Workload Identity)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Any

from app.config import settings
from app.observability.logger import log_dataflow, log_error, log_event_panel


_cached_client: Any | None = None


def _client():
    """Build (and cache) a google-cloud-storage Client based on env config."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    from google.cloud import storage  # local import → optional dep

    if settings.gcs_key_base64:
        try:
            decoded = base64.b64decode(settings.gcs_key_base64, validate=True)
            info = json.loads(decoded)
        except (binascii.Error, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"GCS_KEY_BASE64 is set but cannot be decoded as a service-account JSON: {exc}"
            ) from exc

        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(info)
        project = settings.gcs_project_id or info.get("project_id")
        log_dataflow(
            "gcs.auth",
            f"using GCS_KEY_BASE64 (sa={info.get('client_email', '?')}, project={project})",
        )
        _cached_client = storage.Client(credentials=creds, project=project)
    else:
        # Falls back to GOOGLE_APPLICATION_CREDENTIALS (file path) or ADC.
        log_dataflow(
            "gcs.auth",
            f"using GOOGLE_APPLICATION_CREDENTIALS / ADC "
            f"(project={settings.gcs_project_id or 'auto'})",
        )
        _cached_client = storage.Client(project=settings.gcs_project_id or None)
    return _cached_client


def _upload_bytes_sync(
    *, bucket: str, path: str, data: bytes, content_type: str
) -> str:
    blob = _client().bucket(bucket).blob(path)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{bucket}/{path}"


async def upload_bytes(
    *, path: str, data: bytes, content_type: str = "application/octet-stream"
) -> str | None:
    """Upload one object to the configured GCS bucket. Returns ``gs://`` URI."""
    if not settings.gcs_recordings_enabled or not settings.gcs_bucket:
        log_dataflow("gcs.skipped", "GCS recordings disabled", level="debug")
        return None
    try:
        uri = await asyncio.to_thread(
            _upload_bytes_sync,
            bucket=settings.gcs_bucket,
            path=path,
            data=data,
            content_type=content_type,
        )
        log_dataflow("gcs.uploaded", f"{uri} ({len(data)}b)")
        return uri
    except Exception as exc:
        log_error(
            "GCS UPLOAD FAILED",
            str(exc),
            {"bucket": settings.gcs_bucket, "path": path, "size": len(data)},
        )
        return None


async def upload_call_recording(
    *,
    folder: str,
    mixed_wav: bytes | None,
    metadata: dict[str, Any],
) -> dict[str, str | None]:
    """Upload one ``recording.wav`` (timeline-mixed both sides) + metadata.json.

    Returns::
        {
            "folder":    "gs://jurinex-voice/2026-04-27/14-32-05_CAxxx",
            "recording": "gs://.../recording.wav",
            "metadata":  "gs://.../metadata.json",
        }
    """
    if not settings.gcs_recordings_enabled or not settings.gcs_bucket:
        return {"folder": None, "recording": None, "metadata": None}

    log_event_panel(
        "GCS RECORDING UPLOAD",
        {
            "Bucket": settings.gcs_bucket,
            "Folder": folder,
            "Recording bytes": len(mixed_wav) if mixed_wav else 0,
        },
        style="cyan",
        icon_key="db",
    )

    uploads = await asyncio.gather(
        upload_bytes(
            path=f"{folder}/recording.wav",
            data=mixed_wav or b"",
            content_type="audio/wav",
        ) if mixed_wav else asyncio.sleep(0, result=None),
        upload_bytes(
            path=f"{folder}/metadata.json",
            data=json.dumps(metadata, default=str, indent=2).encode("utf-8"),
            content_type="application/json",
        ),
    )

    return {
        "folder": f"gs://{settings.gcs_bucket}/{folder}",
        "recording": uploads[0],
        "metadata": uploads[1],
    }
