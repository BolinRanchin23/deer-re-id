"""Pure request logic shared by the Vercel HTTP function and tests."""

import hmac
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .client import AuthenticationError, RevealClient, RevealError
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
        return (200 if result.failed == 0 else 207), {
            "ok": result.failed == 0,
            "downloaded": result.downloaded,
            "skipped": result.skipped,
            "failed": result.failed,
        }
    except AuthenticationError:
        return 502, {"ok": False, "error": "Tactacam authentication failed"}
    except (RevealError, StorageError):
        return 502, {"ok": False, "error": "upstream service failed"}
    except ValueError:
        return 503, {"ok": False, "error": "invalid environment configuration"}


def _bounded_int(value: Optional[str], default: int, minimum: int, maximum: int) -> int:
    number = default if value is None else int(value)
    if number < minimum or number > maximum:
        raise ValueError("integer environment setting is out of range")
    return number
