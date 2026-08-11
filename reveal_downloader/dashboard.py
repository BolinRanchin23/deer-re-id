"""Durable, privacy-preserving dashboard state and public API helpers."""

from datetime import datetime, timezone
import hashlib
import hmac
import re
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
import uuid

from .archive import detected_image_extension
from .supabase import StorageError, SupabaseArchive

MAX_RUNS = 20
MAX_RECENT_UNITS = 10
STATUS_DEADLINE_SECONDS = 8.0
PREVIEW_TOKEN_SECONDS = 300
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
_UTC_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)
_RUN_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_PREVIEW_PATH = re.compile(
    r"\A[A-Za-z0-9-][A-Za-z0-9._-]{0,63}@[0-9a-f]{64}/"
    r"\d{4}/\d{2}/\d{2}/\d{8}T\d{6}Z_"
    r"[A-Za-z0-9-][A-Za-z0-9._-]{0,63}@[0-9a-f]{64}\.(?:jpg|png)\Z"
)


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strict_utc_timestamp(value: Any) -> str:
    """Validate and normalize one bounded UTC ISO-8601 timestamp."""
    if not isinstance(value, str) or len(value) > 32 or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise StorageError("Dashboard status is unavailable")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise StorageError("Dashboard status is unavailable") from exc
    return _isoformat(parsed)


def record_dashboard_run(
    archive: Any,
    *,
    status: str,
    downloaded: int,
    skipped: int,
    failed: int,
    archive_units: Iterable[Mapping[str, str]] = (),
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Persist one immutable, bounded private run record."""
    if status not in {"healthy", "degraded", "error"}:
        raise ValueError("invalid dashboard status")
    downloaded_count = max(0, int(downloaded))
    skipped_count = max(0, int(skipped))
    verified = downloaded_count + skipped_count
    recent_units = []
    seen_paths = set()
    for unit in archive_units:
        if not isinstance(unit, Mapping):
            continue
        object_path = unit.get("object_path")
        if not isinstance(object_path, str) or object_path in seen_paths:
            continue
        seen_paths.add(object_path)
        stored_unit = {"object_path": object_path}
        try:
            stored_unit["captured_at"] = _strict_utc_timestamp(unit.get("captured_at"))
        except StorageError:
            pass
        recent_units.append(stored_unit)
        if len(recent_units) >= MAX_RECENT_UNITS:
            break
    run = {
        "version": 1,
        "id": uuid.uuid4().hex,
        "finished_at": _isoformat(now or datetime.now(timezone.utc)),
        "status": status,
        "downloaded": downloaded_count,
        "skipped": skipped_count,
        "failed": max(0, int(failed)),
        "verified": {"image": verified, "metadata": verified, "checksum": verified},
        "recent_units": recent_units,
    }
    archive.write_dashboard_run(run)
    return run


def _safe_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _sanitize_run(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        raise StorageError("Dashboard status is unavailable")
    verified = value.get("verified") if isinstance(value.get("verified"), Mapping) else {}
    status = value.get("status")
    if status not in {"healthy", "degraded", "error"}:
        status = "unknown"
    return {
        "finished_at": _strict_utc_timestamp(value.get("finished_at")),
        "status": status,
        "downloaded": _safe_count(value.get("downloaded")),
        "skipped": _safe_count(value.get("skipped")),
        "failed": _safe_count(value.get("failed")),
        "verified": {
            "image": _safe_count(verified.get("image")),
            "metadata": _safe_count(verified.get("metadata")),
            "checksum": _safe_count(verified.get("checksum")),
        },
    }


def handle_status(
    environ: Mapping[str, str],
    *,
    archive_factory: Callable[[str, str, str], Any] = SupabaseArchive,
    now: Optional[float] = None,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Load private immutable records and return a strict public projection."""
    url = environ.get("SUPABASE_URL", "")
    key = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        return 503, {"ok": False, "error": "status unavailable"}
    clock = (lambda: now) if now is not None else time.monotonic
    try:
        archive = archive_factory(url, key, environ.get("SUPABASE_BUCKET", "tactacam-photos"))
        archive.set_deadline(clock() + STATUS_DEADLINE_SECONDS, clock=clock)
        raw_runs = archive.read_dashboard_runs(limit=MAX_RUNS)
        if not isinstance(raw_runs, list):
            raise StorageError("Dashboard status is unavailable")
        runs = [_sanitize_run(run) for run in raw_runs]
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return 503, {"ok": False, "error": "status unavailable"}
    latest = runs[0] if runs else None
    payload = {
        "ok": True,
        "health": latest["status"] if latest else "unknown",
        "updated_at": latest["finished_at"] if latest else None,
        "latest": latest,
        "recent_runs": runs,
    }
    return 200, payload


def handle_preview(
    environ: Mapping[str, str],
    token: str,
    *,
    archive_factory: Callable[[str, str, str], Any] = SupabaseArchive,
    epoch_now: Optional[int] = None,
) -> Tuple[int, str, bytes]:
    """Return one bounded private image selected by a short-lived opaque token."""
    signing_key = _preview_signing_key(environ)
    if environ.get("PREVIEWS_ENABLED", "").lower() != "true" or not signing_key:
        return 404, "application/json", b'{"error":"not found"}'
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    selected = _verify_preview_token(token, signing_key, current)
    if selected is None:
        return 404, "application/json", b'{"error":"not found"}'
    run_id, unit_index = selected
    url = environ.get("SUPABASE_URL", "")
    key = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        return 404, "application/json", b'{"error":"not found"}'
    try:
        archive = archive_factory(url, key, environ.get("SUPABASE_BUCKET", "tactacam-photos"))
        clock = time.monotonic
        archive.set_deadline(clock() + STATUS_DEADLINE_SECONDS, clock=clock)
        raw_runs = archive.read_dashboard_runs(limit=MAX_RUNS)
        run = next(
            value
            for value in raw_runs
            if isinstance(value, Mapping) and value.get("id") == run_id
        )
        units = run.get("recent_units")
        if not isinstance(units, list) or unit_index >= len(units):
            raise StorageError("Preview is unavailable")
        unit = units[unit_index]
        object_path = unit.get("object_path") if isinstance(unit, Mapping) else None
        if not isinstance(object_path, str) or _PREVIEW_PATH.fullmatch(object_path) is None:
            raise StorageError("Preview is unavailable")
        body = archive.read_private_image(object_path, max_bytes=MAX_PREVIEW_BYTES)
        extension = detected_image_extension(body)
        if not object_path.endswith(extension):
            raise StorageError("Preview is unavailable")
    except (AttributeError, OSError, RuntimeError, StopIteration, TypeError, ValueError, StorageError):
        return 404, "application/json", b'{"error":"not found"}'
    return 200, "image/jpeg" if extension == ".jpg" else "image/png", body


def _preview_signing_key(environ: Mapping[str, str]) -> Optional[bytes]:
    secret = environ.get("CRON_SECRET", "")
    return secret.encode("utf-8") if len(secret) >= 16 else None


def _preview_descriptors(raw_runs: Iterable[Any], key: bytes, now: int) -> list[Dict[str, str]]:
    previews = []
    for run in raw_runs:
        if not isinstance(run, Mapping):
            continue
        run_id = run.get("id")
        units = run.get("recent_units")
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None or not isinstance(units, list):
            continue
        for index, unit in enumerate(units[:MAX_RECENT_UNITS]):
            if not isinstance(unit, Mapping):
                continue
            path = unit.get("object_path")
            if not isinstance(path, str) or _PREVIEW_PATH.fullmatch(path) is None:
                continue
            descriptor = {
                "url": f"/api/preview?token={_sign_preview_token(run_id, index, now + PREVIEW_TOKEN_SECONDS, key)}"
            }
            try:
                descriptor["captured_at"] = _strict_utc_timestamp(unit.get("captured_at"))
            except StorageError:
                pass
            previews.append(descriptor)
            if len(previews) >= 5:
                return previews
        if previews:
            return previews
    return previews


def _sign_preview_token(run_id: str, index: int, expires: int, key: bytes) -> str:
    token_payload = f"{expires}.{run_id}.{index}"
    signature = hmac.new(key, token_payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{token_payload}.{signature}"


def _verify_preview_token(token: str, key: bytes, now: int) -> Optional[Tuple[str, int]]:
    if not isinstance(token, str) or len(token) > 160:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    expires_text, run_id, index_text, supplied = parts
    if (
        not expires_text.isdigit()
        or _RUN_ID.fullmatch(run_id) is None
        or not index_text.isdigit()
        or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
    ):
        return None
    expires = int(expires_text)
    index = int(index_text)
    if expires < now or expires > now + PREVIEW_TOKEN_SECONDS + 30 or index >= MAX_RECENT_UNITS:
        return None
    token_payload = f"{expires}.{run_id}.{index}"
    expected = hmac.new(key, token_payload.encode("ascii"), hashlib.sha256).hexdigest()
    return (run_id, index) if hmac.compare_digest(expected, supplied) else None
