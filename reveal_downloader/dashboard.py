"""Durable, privacy-preserving dashboard state and public API helpers."""

from datetime import datetime, timezone
import re
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
import uuid

from .supabase import StorageError, SupabaseArchive

MAX_RUNS = 20
MAX_RECENT_UNITS = 10
STATUS_DEADLINE_SECONDS = 8.0
_UTC_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
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
    return 200, {
        "ok": True,
        "health": latest["status"] if latest else "unknown",
        "updated_at": latest["finished_at"] if latest else None,
        "latest": latest,
        "recent_runs": runs,
    }
