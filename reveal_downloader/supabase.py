"""Supabase Storage archive used by the Vercel cron function."""

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .archive import SyncResult, detected_image_extension, relative_photo_path

DEFAULT_STORAGE_TIMEOUT = 8.0
MIN_REQUEST_BUDGET = 0.1
MAX_STORAGE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_STORAGE_JSON_BYTES = 64 * 1024
MAX_STATUS_RECORD_BYTES = 64 * 1024
_STATUS_OBJECT_NAME = re.compile(r"\A\d{8}T\d{6}Z-[0-9a-f]{32}\.json\Z")


class StorageError(RuntimeError):
    """A Supabase Storage operation failed."""


@dataclass(frozen=True)
class StorageResponse:
    status: int
    body: bytes


class StorageTransport:
    """Minimal HTTP transport for Supabase Storage."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._deadline: Optional[float] = None

    def set_deadline(
        self,
        deadline: Optional[float],
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._deadline = deadline
        if clock is not None:
            self._clock = clock

    def _timeout(self) -> float:
        if self._deadline is None:
            return DEFAULT_STORAGE_TIMEOUT
        remaining = self._deadline - self._clock()
        if remaining < MIN_REQUEST_BUDGET:
            raise StorageError("Vercel sync deadline reached")
        return min(DEFAULT_STORAGE_TIMEOUT, remaining)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        max_response_bytes: int = MAX_STORAGE_RESPONSE_BYTES,
    ) -> StorageResponse:
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=self._timeout()) as response:
                response_body = response.read(max_response_bytes + 1)
                if len(response_body) > max_response_bytes:
                    raise StorageError("Supabase response exceeds size limit")
                return StorageResponse(response.status, response_body)
        except HTTPError as exc:
            response_body = exc.read(max_response_bytes + 1)
            if len(response_body) > max_response_bytes:
                raise StorageError("Supabase response exceeds size limit")
            return StorageResponse(exc.code, response_body)
        except (URLError, TimeoutError) as exc:
            raise StorageError(f"Could not reach Supabase Storage: {exc}") from exc


class SupabaseArchive:
    """Archive Reveal photos into a private Supabase Storage bucket."""

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
        self.bucket = bucket.strip("/")
        if not self.bucket:
            raise ValueError("Supabase bucket name is required")
        self._transport = transport or StorageTransport()
        self._headers = {
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
        }
        self._bucket_ready = False
        self.last_archive_units = []
        self.progress_downloaded = 0
        self.progress_skipped = 0
        self.progress_failed = 0

    def set_deadline(
        self,
        deadline: Optional[float],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Apply one shared serverless deadline to all Storage requests."""
        transport_deadline = getattr(self._transport, "set_deadline", None)
        if callable(transport_deadline):
            transport_deadline(deadline, clock=clock)

    def sync(
        self,
        client: Any,
        *,
        camera_id: Optional[str] = None,
        page_size: int = 100,
        max_pages: int = 2,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> SyncResult:
        def check_deadline() -> None:
            _check_deadline(deadline, clock)

        check_deadline()
        transport_deadline = getattr(self._transport, "set_deadline", None)
        if callable(transport_deadline):
            transport_deadline(deadline, clock=clock)
        client_deadline = getattr(client, "set_deadline", None)
        if callable(client_deadline):
            client_deadline(deadline, clock=clock)
        self._ensure_bucket()
        self.last_archive_units = []
        self.progress_downloaded = 0
        self.progress_skipped = 0
        self.progress_failed = 0
        page = 0
        seen_pages = set()
        while max_pages <= 0 or page < max_pages:
            check_deadline()
            photos = client.get_photos(size=page_size, page=page, camera_id=camera_id)
            if not photos:
                break
            fingerprint = tuple(
                str(photo.get("photoId") or photo.get("photoUrl") or photo.get("filename"))
                for photo in photos
            )
            if fingerprint in seen_pages:
                break
            seen_pages.add(fingerprint)

            for photo in photos:
                try:
                    check_deadline()
                    image_path = relative_photo_path(photo).as_posix()
                    metadata_path = _replace_suffix(image_path, ".json")
                    checksum_path = _replace_suffix(image_path, ".sha256")
                    marker = self._download(checksum_path)
                    if marker is not None:
                        check_deadline()
                        image_body = self._download(image_path)
                        check_deadline()
                        metadata_body = self._download(metadata_path)
                        if _cloud_entry_complete(
                            marker, image_body, metadata_body, photo, image_path
                        ):
                            self.progress_skipped += 1
                            self._remember_unit(image_path, photo)
                            continue
                        check_deadline()
                        # Invalidate the commit record before any repair can fail.
                        self._delete(checksum_path)
                    photo_url = photo.get("photoUrl")
                    if not photo_url:
                        raise ValueError("photo has no photoUrl")
                    check_deadline()
                    image = client.download(photo_url)
                    if detected_image_extension(image) != _path_suffix(image_path):
                        raise ValueError("downloaded image type does not match object extension")
                    metadata = json.dumps(photo, indent=2, sort_keys=True).encode("utf-8")
                    checksum = (hashlib.sha256(image).hexdigest() + "\n").encode("ascii")
                    check_deadline()
                    self._upload(image_path, image, _content_type(image_path))
                    check_deadline()
                    self._upload(metadata_path, metadata, "application/json")
                    # The checksum is the completion marker and must be uploaded last.
                    check_deadline()
                    self._upload(checksum_path, checksum, "text/plain")
                    check_deadline()
                    stored_image = self._download(image_path)
                    check_deadline()
                    stored_metadata = self._download(metadata_path)
                    check_deadline()
                    stored_marker = self._download(checksum_path)
                    if stored_marker is None or not _cloud_entry_complete(
                        stored_marker, stored_image, stored_metadata, photo, image_path
                    ):
                        self._delete(checksum_path)
                        raise ValueError("uploaded archive unit failed read-back verification")
                    self.progress_downloaded += 1
                    self._remember_unit(image_path, photo)
                except StorageError:
                    self.progress_failed += 1
                    raise
                except (KeyError, OSError, RuntimeError, ValueError, TypeError):
                    self.progress_failed += 1

            if len(photos) < page_size:
                break
            page += 1
        return SyncResult(
            downloaded=self.progress_downloaded,
            skipped=self.progress_skipped,
            failed=self.progress_failed,
        )

    def _remember_unit(self, image_path: str, photo: Dict[str, Any]) -> None:
        if len(self.last_archive_units) >= 10:
            return
        captured_at = photo.get("photoDateUtc")
        self.last_archive_units.append(
            {
                "object_path": image_path,
                "captured_at": captured_at if isinstance(captured_at, str) else "",
            }
        )

    def read_dashboard_runs(self, limit: int = 20) -> list[Dict[str, Any]]:
        """List and read bounded immutable run records, newest first."""
        self._ensure_bucket()
        bounded_limit = max(1, min(20, int(limit)))
        request_body = json.dumps(
            {
                "prefix": "_status/runs",
                "limit": bounded_limit,
                "offset": 0,
                "sortBy": {"column": "name", "order": "desc"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._transport.request(
            "POST",
            f"{self.base_url}/storage/v1/object/list/{quote(self.bucket, safe='')}",
            headers={**self._headers, "Content-Type": "application/json"},
            body=request_body,
            max_response_bytes=MAX_STORAGE_JSON_BYTES,
        )
        if response.status != 200:
            raise StorageError(f"Supabase status list failed with HTTP {response.status}")
        try:
            entries = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError("Dashboard status is unavailable") from exc
        if not isinstance(entries, list) or len(entries) > bounded_limit:
            raise StorageError("Dashboard status is unavailable")
        runs = []
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or _STATUS_OBJECT_NAME.fullmatch(name) is None:
                raise StorageError("Dashboard status is unavailable")
            body = self._download(
                f"_status/runs/{name}", max_bytes=MAX_STATUS_RECORD_BYTES
            )
            if body is None:
                raise StorageError("Dashboard status is unavailable")
            try:
                run = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StorageError("Dashboard status is unavailable") from exc
            if not isinstance(run, dict):
                raise StorageError("Dashboard status is unavailable")
            runs.append(run)
        return runs

    def write_dashboard_run(self, run: Dict[str, Any]) -> None:
        """Persist one run under a unique immutable private object name."""
        self._ensure_bucket()
        finished_at = run.get("finished_at")
        run_id = run.get("id")
        if not isinstance(finished_at, str) or not isinstance(run_id, str):
            raise StorageError("Dashboard run is invalid")
        compact = finished_at.replace("-", "").replace(":", "")
        name = f"{compact}-{run_id}.json"
        if _STATUS_OBJECT_NAME.fullmatch(name) is None:
            raise StorageError("Dashboard run is invalid")
        body = json.dumps(run, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_STATUS_RECORD_BYTES:
            raise StorageError("Dashboard run exceeds size limit")
        self._upload(
            f"_status/runs/{name}", body, "application/json", upsert=False
        )


    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        bucket_url = f"{self.base_url}/storage/v1/bucket/{quote(self.bucket, safe='')}"
        response = self._transport.request(
            "GET", bucket_url, headers=self._headers, max_response_bytes=MAX_STORAGE_JSON_BYTES
        )
        bucket_missing = response.status == 404
        if response.status == 400:
            try:
                error_payload = json.loads(response.body.decode("utf-8"))
                error_text = " ".join(
                    str(error_payload.get(field, ""))
                    for field in ("error", "message")
                ).lower()
                bucket_missing = "bucket" in error_text and "not found" in error_text
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                bucket_missing = False
        if bucket_missing:
            body = json.dumps(
                {"id": self.bucket, "name": self.bucket, "public": False}
            ).encode("utf-8")
            headers = {**self._headers, "Content-Type": "application/json"}
            response = self._transport.request(
                "POST",
                f"{self.base_url}/storage/v1/bucket",
                headers=headers,
                body=body,
                max_response_bytes=MAX_STORAGE_JSON_BYTES,
            )
        elif response.status == 200:
            try:
                bucket = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StorageError("Supabase bucket must explicitly be private") from exc
            if not isinstance(bucket, dict) or bucket.get("public") is not False:
                raise StorageError("Supabase bucket must explicitly be private")
        if response.status not in {200, 201}:
            raise StorageError(f"Supabase bucket setup failed with HTTP {response.status}")
        self._bucket_ready = True

    def _download(
        self, object_path: str, *, max_bytes: int = MAX_STORAGE_RESPONSE_BYTES
    ) -> Optional[bytes]:
        url = self._object_url("authenticated", object_path)
        response = self._transport.request(
            "GET", url, headers=self._headers, max_response_bytes=max_bytes
        )
        if response.status == 200:
            return response.body
        if response.status == 404 or _is_missing_object_response(response):
            return None
        raise StorageError(f"Supabase object read failed with HTTP {response.status}")

    def _delete(self, object_path: str) -> None:
        response = self._transport.request(
            "DELETE", self._object_url("", object_path), headers=self._headers
        )
        if response.status not in {200, 204, 404}:
            raise StorageError(f"Supabase object delete failed with HTTP {response.status}")

    def _upload(
        self, object_path: str, body: bytes, content_type: str, *, upsert: bool = True
    ) -> None:
        headers = {
            **self._headers,
            "Content-Type": content_type,
            "x-upsert": "true" if upsert else "false",
        }
        response = self._transport.request(
            "POST", self._object_url("", object_path), headers=headers, body=body
        )
        if response.status not in {200, 201}:
            raise StorageError(f"Supabase upload failed with HTTP {response.status}")

    def _object_url(self, operation: str, object_path: str) -> str:
        prefix = f"{self.base_url}/storage/v1/object"
        if operation:
            prefix += f"/{operation}"
        encoded = quote(f"{self.bucket}/{object_path}", safe="/")
        return f"{prefix}/{encoded}"


def _project_origin(value: str) -> str:
    candidate = value.strip().rstrip("/")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parts = urlsplit(candidate)
    if not parts.scheme or not parts.netloc:
        raise ValueError("A valid Supabase project URL is required")
    if parts.scheme.lower() != "https":
        raise ValueError("Supabase project URL must use HTTPS")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _is_missing_object_response(response: StorageResponse) -> bool:
    if response.status != 400:
        return False
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    status_code = str(payload.get("statusCode", ""))
    error = str(payload.get("error", "")).lower()
    message = str(payload.get("message", "")).lower()
    return (
        status_code == "404"
        or error in {"not_found", "object_not_found"}
        or "object not found" in message
    )


def _path_suffix(path: str) -> str:
    filename = path.rsplit("/", 1)[-1]
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _replace_suffix(path: str, suffix: str) -> str:
    head, _, filename = path.rpartition("/")
    stem = filename.rsplit(".", 1)[0]
    replacement = stem + suffix
    return f"{head}/{replacement}" if head else replacement


def _content_type(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


def _cloud_entry_complete(
    marker: bytes,
    image: Optional[bytes],
    metadata: Optional[bytes],
    photo: Dict[str, Any],
    image_path: str,
) -> bool:
    if image is None or metadata is None or re.fullmatch(rb"[0-9a-f]{64}\n", marker) is None:
        return False
    try:
        stored_metadata = json.loads(metadata.decode("utf-8"))
        if detected_image_extension(image) != _path_suffix(image_path):
            return False
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    expected = marker[:-1].decode("ascii")
    actual = hashlib.sha256(image).hexdigest()
    return (
        isinstance(stored_metadata, dict)
        and stored_metadata == photo
        and hmac.compare_digest(expected, actual)
    )


def _check_deadline(deadline: Optional[float], clock: Callable[[], float]) -> None:
    if deadline is not None and deadline - clock() < MIN_REQUEST_BUDGET:
        raise StorageError("Vercel sync deadline reached")
