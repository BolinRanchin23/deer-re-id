"""Bounded fulfillment worker for HD requests queued before live submission existed."""

import json
import os
import time
from typing import Any, Callable, Mapping

from .catalog import SupabaseCatalog
from .client import HDRequestRejected, RevealClient, RevealError
from .supabase import StorageError

HD_QUEUE_DEADLINE_SECONDS = 240.0


def run_worker(
    environ: Mapping[str, str],
    *,
    max_requests: int = 20,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    reveal_factory: Callable[[str, str], Any] = RevealClient,
) -> dict[str, Any]:
    """Drain a bounded set of durable legacy requests through Reveal's official API."""
    required = (
        "TACTACAM_USERNAME", "TACTACAM_PASSWORD", "SUPABASE_URL", "SUPABASE_SECRET_KEY"
    )
    if any(not environ.get(name) for name in required):
        raise ValueError("HD request worker configuration is incomplete")
    if isinstance(max_requests, bool) or not 1 <= int(max_requests) <= 50:
        raise ValueError("max_requests must be between 1 and 50")

    clock = time.monotonic
    deadline = clock() + HD_QUEUE_DEADLINE_SECONDS
    catalog = catalog_factory(
        environ["SUPABASE_URL"], environ["SUPABASE_SECRET_KEY"],
        environ.get("SUPABASE_BUCKET", "tactacam-photos"),
    )
    catalog.set_deadline(deadline, clock=clock)
    client = reveal_factory(environ["TACTACAM_USERNAME"], environ["TACTACAM_PASSWORD"])
    client.set_deadline(deadline, clock=clock)
    submitted = 0
    failed = 0
    unknown = 0
    empty = False

    for _ in range(int(max_requests)):
        claim = catalog.claim_queued_hd_request()
        if not isinstance(claim, Mapping) or not claim.get("ok"):
            raise StorageError("HD queue claim failed")
        if claim.get("empty"):
            empty = True
            break
        token = claim.get("request_token")
        photo_id = claim.get("provider_photo_id")
        if not isinstance(token, str) or not token or not isinstance(photo_id, str) or not photo_id:
            raise StorageError("HD queue claim is malformed")
        try:
            client.request_hd_photos([photo_id])
        except HDRequestRejected:
            result = catalog.fail_hd_request(token, "provider_rejected")
            if not isinstance(result, Mapping) or not result.get("ok"):
                raise StorageError("HD request failure could not be recorded")
            failed += 1
            continue
        except (RevealError, OSError, RuntimeError, ValueError):
            result = catalog.mark_hd_request_unknown(token, "provider_outcome_unknown")
            if not isinstance(result, Mapping) or not result.get("ok"):
                raise StorageError("ambiguous HD request outcome could not be recorded")
            unknown += 1
            continue
        result = catalog.complete_hd_request(token)
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise StorageError("HD request completion could not be recorded")
        submitted += 1

    return {
        "ok": failed == 0 and unknown == 0,
        "submitted": submitted,
        "failed": failed,
        "unknown": unknown,
        "empty": empty,
    }


def main() -> int:
    result = run_worker(os.environ, max_requests=int(os.environ.get("HD_REQUEST_BATCH", "20")))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
