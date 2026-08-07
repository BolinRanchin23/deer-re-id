"""Pure request logic shared by the Vercel HTTP function and tests."""

import hmac
import re
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .client import AuthenticationError, RevealClient, RevealError
from .dashboard import record_dashboard_run
from .supabase import StorageError, SupabaseArchive


def handle_sync(
    environ: Mapping[str, str],
    authorization: Optional[str],
    *,
    client_factory: Callable[[str, str], Any] = RevealClient,
    archive_factory: Callable[[str, str, str], Any] = SupabaseArchive,
) -> Tuple[int, Dict[str, Any]]:
    """Validate a cron request and run one bounded Supabase sync."""
    cron_secret = environ.get("CRON_SECRET", "")
    if not cron_secret:
        return 503, {"ok": False, "error": "CRON_SECRET is not configured"}
    if len(cron_secret) < 16:
        return 503, {
            "ok": False,
            "error": "CRON_SECRET must be at least 16 characters",
        }
    expected = f"Bearer {cron_secret}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        return 401, {"ok": False, "error": "unauthorized"}

    required = (
        "TACTACAM_USERNAME",
        "TACTACAM_PASSWORD",
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        return 503, {
            "ok": False,
            "error": "missing environment variables",
            "missing": missing,
        }

    archive = None
    try:
        page_size = _bounded_int(environ.get("REVEAL_PAGE_SIZE"), 100, 1, 1000)
        max_pages = _bounded_int(environ.get("REVEAL_MAX_PAGES"), 2, 1, 100)
        client = client_factory(
            environ["TACTACAM_USERNAME"], environ["TACTACAM_PASSWORD"]
        )
        archive = archive_factory(
            environ["SUPABASE_URL"],
            environ["SUPABASE_SECRET_KEY"],
            environ.get("SUPABASE_BUCKET", "tactacam-photos"),
        )
        result = archive.sync(
            client,
            page_size=page_size,
            max_pages=max_pages,
            deadline=time.monotonic() + 45,
        )
        status_recorded = _record_status(
            archive,
            status="healthy" if result.failed == 0 else "degraded",
            downloaded=result.downloaded,
            skipped=result.skipped,
            failed=result.failed,
        )
        return (200 if result.failed == 0 else 207), {
            "ok": result.failed == 0,
            "downloaded": result.downloaded,
            "skipped": result.skipped,
            "failed": result.failed,
            "status_recorded": status_recorded,
        }
    except AuthenticationError:
        _record_failure(archive)
        return 502, {"ok": False, "error": "Tactacam authentication failed"}
    except StorageError as exc:
        _record_failure(archive)
        payload = {
            "ok": False,
            "error": "storage service failed",
            "storage_stage": _storage_stage(exc),
        }
        http_status = _storage_http_status(exc)
        if http_status is not None:
            payload["storage_http_status"] = http_status
        return 502, payload
    except RevealError:
        _record_failure(archive)
        return 502, {"ok": False, "error": "Reveal service failed"}
    except ValueError:
        _record_failure(archive)
        return 503, {"ok": False, "error": "invalid environment configuration"}


def _storage_stage(exc: StorageError) -> str:
    message = str(exc).lower()
    if "bucket" in message:
        return "bucket_access"
    if "upload" in message:
        return "object_write"
    if "read" in message or "list" in message:
        return "object_read"
    if "deadline" in message or "budget" in message:
        return "network_or_deadline"
    return "storage_access"


def _storage_http_status(exc: StorageError) -> Optional[int]:
    match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(exc))
    return int(match.group(1)) if match else None


def _record_failure(archive: Any) -> None:
    if archive is None:
        return
    _record_status(
        archive,
        status="error",
        downloaded=getattr(archive, "progress_downloaded", 0),
        skipped=getattr(archive, "progress_skipped", 0),
        failed=max(1, getattr(archive, "progress_failed", 0)),
    )


def _record_status(
    archive: Any,
    *,
    status: str,
    downloaded: int,
    skipped: int,
    failed: int,
) -> bool:
    try:
        record_dashboard_run(
            archive,
            status=status,
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            archive_units=getattr(archive, "last_archive_units", ()),
        )
        return True
    except (OSError, RuntimeError, TypeError, ValueError, StorageError):
        # Telemetry must never replace the endpoint's true synchronization result.
        return False


def _bounded_int(value: Optional[str], default: int, minimum: int, maximum: int) -> int:
    number = default if value is None else int(value)
    if number < minimum or number > maximum:
        raise ValueError("integer environment setting is out of range")
    return number
