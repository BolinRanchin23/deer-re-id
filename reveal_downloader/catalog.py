"""Authenticated private photo library and camera-map helpers."""

import hashlib
import hmac
import json
import re
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
import uuid

from .archive import detected_image_extension
from .supabase import (
    MAX_STORAGE_JSON_BYTES,
    StorageError,
    StorageTransport,
    SupabaseArchive,
    _postgrest_auth_headers,
    _project_origin,
)

LIBRARY_DEADLINE_SECONDS = 8.0
LIBRARY_PREVIEW_SECONDS = 300
LIBRARY_REVIEW_SECONDS = 900
MAX_LIBRARY_PHOTOS = 60
MAX_LIBRARY_PREVIEW_BYTES = 8 * 1024 * 1024
GATE1_MODEL_NAME = "SpeciesNet"
GATE1_MODEL_VERSION = "4.0.3a"
_UUID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_OBJECT_PATH = re.compile(
    r"\A[A-Za-z0-9-][A-Za-z0-9._-]{0,63}@[0-9a-f]{64}/"
    r"\d{4}/\d{2}/\d{2}/\d{8}T\d{6}Z_"
    r"[A-Za-z0-9-][A-Za-z0-9._-]{0,63}@[0-9a-f]{64}\.(?:jpg|png)\Z"
)
_MAPBOX_TOKEN = re.compile(r"\Apk\.[A-Za-z0-9._-]{8,500}\Z")


class SupabaseCatalog:
    """Minimal service-role PostgREST client for private DeerID RPCs."""

    def __init__(
        self,
        base_url: str,
        secret_key: str,
        bucket: str = "tactacam-photos",
        *,
        transport: Optional[Any] = None,
    ) -> None:
        if not secret_key:
            raise ValueError("Supabase secret key is required")
        self.base_url = _project_origin(base_url)
        self._transport = transport or StorageTransport()
        self._headers = {
            **_postgrest_auth_headers(secret_key),
            "Content-Type": "application/json",
        }
        self._archive = SupabaseArchive(
            self.base_url, secret_key, bucket, transport=self._transport
        )

    def set_deadline(self, deadline: float, *, clock: Callable[[], float]) -> None:
        self._archive.set_deadline(deadline, clock=clock)

    def _rpc(self, name: str, payload: Dict[str, Any]) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self._transport.request(
            "POST",
            f"{self.base_url}/rest/v1/rpc/{name}",
            headers=self._headers,
            body=body,
            max_response_bytes=MAX_STORAGE_JSON_BYTES,
        )
        if response.status != 200:
            raise StorageError(
                f"Supabase private catalog read failed with HTTP {response.status}",
                http_status=response.status,
            )
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError("Private catalog is unavailable") from exc

    def read_library(self, limit: int = MAX_LIBRARY_PHOTOS) -> Any:
        return self._rpc("deerid_private_library", {"p_limit": max(1, min(60, int(limit)))})

    def read_camera_map(self) -> Any:
        return self._rpc("deerid_private_camera_map", {})

    def read_gate1_funnel(self, model_name: str, model_version: str) -> Any:
        return self._rpc(
            "deerid_gate1_funnel",
            {"p_model_name": model_name, "p_model_version": model_version},
        )

    def resolve_media_object(self, media_id: str) -> Any:
        return self._rpc("deerid_private_media_object", {"p_media_id": media_id})

    def record_review(
        self, media_id: str, assessment_id: int, review_version: int, action: str, note: str
    ) -> Any:
        return self._rpc(
            "deerid_record_review_decision",
            {
                "p_media_id": media_id, "p_assessment_id": assessment_id,
                "p_review_version": review_version, "p_action": action, "p_note": note or None,
            },
        )

    def read_gate1_pending(self, model_name: str, model_version: str, limit: int = 60) -> Any:
        return self._rpc(
            "deerid_gate1_pending",
            {"p_model_name": model_name, "p_model_version": model_version, "p_limit": limit},
        )

    def record_gate1_batch(
        self, model_name: str, model_version: str, results: list[dict[str, Any]]
    ) -> Any:
        return self._rpc(
            "deerid_record_gate1_batch",
            {"p_model_name": model_name, "p_model_version": model_version, "p_results": results},
        )

    def read_private_image(self, object_path: str, *, max_bytes: int) -> bytes:
        return self._archive.read_private_image(object_path, max_bytes=max_bytes)


def handle_library(
    environ: Mapping[str, str],
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    now: Optional[float] = None,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Return the open-prototype catalog through the server-side service key."""
    signing_key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    url = environ.get("SUPABASE_URL", "")
    key = environ.get("SUPABASE_SECRET_KEY", "")
    if signing_key is None or not url or not key:
        return 404, {"ok": False, "error": "not found"}
    clock = (lambda: now) if now is not None else time.monotonic
    try:
        catalog = catalog_factory(
            url, key, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        catalog.set_deadline(clock() + LIBRARY_DEADLINE_SECONDS, clock=clock)
        photos = _sanitize_photos(catalog.read_library(MAX_LIBRARY_PHOTOS), signing_key, current)
        cameras = _sanitize_cameras(catalog.read_camera_map())
        pipeline = _sanitize_pipeline(
            catalog.read_gate1_funnel(GATE1_MODEL_NAME, GATE1_MODEL_VERSION)
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return 503, {"ok": False, "error": "library unavailable"}
    payload: Dict[str, Any] = {
        "ok": True, "photos": photos, "cameras": cameras, "pipeline": pipeline
    }
    mapbox_token = environ.get("NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN", "").strip()
    if _MAPBOX_TOKEN.fullmatch(mapbox_token):
        payload["mapbox_access_token"] = mapbox_token
    return 200, payload


def handle_library_preview(
    environ: Mapping[str, str],
    token: str,
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, str, bytes]:
    """Resolve a signed media UUID server-side and proxy one bounded image."""
    key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    media_id = _verify_media_token(token, key, current) if key is not None else None
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if media_id is None or not url or not secret:
        return _not_found()
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        clock = time.monotonic
        catalog.set_deadline(clock() + LIBRARY_DEADLINE_SECONDS, clock=clock)
        selected = catalog.resolve_media_object(media_id)
        if isinstance(selected, list):
            selected = selected[0] if len(selected) == 1 else None
        object_path = selected.get("object_path") if isinstance(selected, Mapping) else None
        expected_type = selected.get("content_type") if isinstance(selected, Mapping) else None
        if not isinstance(object_path, str) or _OBJECT_PATH.fullmatch(object_path) is None:
            raise StorageError("Library preview is unavailable")
        body = catalog.read_private_image(object_path, max_bytes=MAX_LIBRARY_PREVIEW_BYTES)
        extension = detected_image_extension(body)
        content_type = "image/jpeg" if extension == ".jpg" else "image/png"
        if not object_path.endswith(extension) or expected_type != content_type:
            raise StorageError("Library preview is unavailable")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return _not_found()
    return 200, content_type, body


def handle_review(
    environ: Mapping[str, str],
    token: str,
    action: str,
    note: str = "",
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Record one bounded human decision for a model-selected photo."""
    if action not in {"request_hd", "keep_for_identity", "not_useful", "defer"}:
        return 400, {"ok": False, "error": "invalid action"}
    if not isinstance(note, str) or len(note) > 500:
        return 400, {"ok": False, "error": "invalid note"}
    key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = _verify_review_token(token, key, current) if key is not None else None
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if capability is None or not url or not secret:
        return 404, {"ok": False, "error": "not found"}
    media_id, assessment_id, review_version = capability
    try:
        catalog = catalog_factory(url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos"))
        result = catalog.record_review(media_id, assessment_id, review_version, action, note.strip())
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise StorageError("Review decision failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return 503, {"ok": False, "error": "review unavailable"}
    return 200, {"ok": True, "media_id": media_id, "action": action}


def _sanitize_photos(value: Any, key: bytes, now: int) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_LIBRARY_PHOTOS:
        raise StorageError("Private library is unavailable")
    output = []
    allowed = {
        "id", "captured_at", "camera_id", "camera_name", "variant", "width", "height",
        "labels", "animals", "hd_photo", "has_headshot", "battery_level", "signal_level",
        "gate1", "review_decision",
    }
    for item in value:
        media_id = item.get("id") if isinstance(item, Mapping) else None
        if not isinstance(media_id, str) or _UUID.fullmatch(media_id) is None:
            raise StorageError("Private library is unavailable")
        safe = {name: item[name] for name in allowed if name in item}
        safe["preview_url"] = f"/api/library_preview?token={_sign_media_token(media_id, now + LIBRARY_PREVIEW_SECONDS, key)}"
        gate1 = safe.get("gate1")
        decision = safe.get("review_decision")
        if (
            isinstance(gate1, Mapping)
            and gate1.get("route") == "review"
            and (decision is None or (isinstance(decision, Mapping) and decision.get("action") == "defer"))
        ):
            assessment_id = gate1.get("id")
            review_version = gate1.get("review_version")
            if not isinstance(assessment_id, int) or assessment_id < 1 or not isinstance(review_version, int) or review_version < 0:
                raise StorageError("Private library is unavailable")
            safe["review_token"] = _sign_review_token(
                media_id, assessment_id, review_version, now + LIBRARY_REVIEW_SECONDS, key
            )
        output.append(safe)
    return output


def _sanitize_cameras(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100:
        raise StorageError("Private camera map is unavailable")
    allowed = {
        "id", "name", "location_name", "latitude", "longitude", "observed_at",
        "battery_level", "signal_level", "hardware_version", "last_seen_at",
    }
    output = []
    for item in value:
        camera_id = item.get("id") if isinstance(item, Mapping) else None
        if not isinstance(camera_id, str) or _UUID.fullmatch(camera_id) is None:
            raise StorageError("Private camera map is unavailable")
        output.append({name: item[name] for name in allowed if name in item})
    return output


def _sanitize_pipeline(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageError("Gate 1 funnel is unavailable")
    model_name = value.get("model_name")
    model_version = value.get("model_version")
    if model_name != GATE1_MODEL_NAME or model_version != GATE1_MODEL_VERSION:
        raise StorageError("Gate 1 funnel is unavailable")
    count_fields = (
        "total_thumbnails", "assessed_thumbnails", "pending_thumbnails",
        "review_representatives", "event_duplicates", "archived",
        "blank_or_below_threshold", "confident_non_target",
        "unresolved_review", "resolved_review",
    )
    output: Dict[str, Any] = {"model_name": model_name, "model_version": model_version}
    for field in count_fields:
        count = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise StorageError("Gate 1 funnel is unavailable")
        output[field] = count
    if output["assessed_thumbnails"] + output["pending_thumbnails"] != output["total_thumbnails"]:
        raise StorageError("Gate 1 funnel is unavailable")
    return output


def _signing_key(environ: Mapping[str, str]) -> Optional[bytes]:
    secret = environ.get("LIBRARY_PREVIEW_SECRET", "")
    return secret.encode("utf-8") if len(secret) >= 16 else None


def _sign_media_token(media_id: str, expires: int, key: bytes) -> str:
    payload = f"{expires}.{media_id}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _sign_review_token(media_id: str, assessment_id: int, review_version: int, expires: int, key: bytes) -> str:
    payload = f"{expires}.review.{media_id}.{assessment_id}.{review_version}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify_review_token(token: str, key: bytes, now: int) -> Optional[Tuple[str, int, int]]:
    if not isinstance(token, str) or len(token) > 210:
        return None
    parts = token.split(".")
    if len(parts) != 6:
        return None
    expires_text, purpose, media_id, assessment_text, version_text, supplied = parts
    if (
        purpose != "review"
        or not expires_text.isdigit()
        or _UUID.fullmatch(media_id) is None
        or not assessment_text.isdigit()
        or not version_text.isdigit()
        or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
    ):
        return None
    expires = int(expires_text)
    if expires < now or expires > now + LIBRARY_REVIEW_SECONDS + 30:
        return None
    assessment_id = int(assessment_text)
    review_version = int(version_text)
    if assessment_id < 1 or review_version < 0:
        return None
    payload = f"{expires}.review.{media_id}.{assessment_id}.{review_version}"
    expected = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return (media_id, assessment_id, review_version) if hmac.compare_digest(expected, supplied) else None


def _verify_media_token(token: str, key: bytes, now: int) -> Optional[str]:
    if not isinstance(token, str) or len(token) > 150:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    expires_text, media_id, supplied = parts
    if (
        not expires_text.isdigit()
        or _UUID.fullmatch(media_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
    ):
        return None
    expires = int(expires_text)
    if expires < now or expires > now + LIBRARY_PREVIEW_SECONDS + 30:
        return None
    payload = f"{expires}.{media_id}"
    expected = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return media_id if hmac.compare_digest(expected, supplied) else None


def _not_found() -> Tuple[int, str, bytes]:
    return 404, "application/json", b'{"error":"not found"}'
