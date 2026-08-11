"""Supabase Storage archive used by the Vercel cron function."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import time
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .archive import (
    SyncResult,
    _metadata_correlates,
    detected_image_extension,
    relative_photo_path,
)

DEFAULT_STORAGE_TIMEOUT = 8.0
MIN_REQUEST_BUDGET = 0.1
MAX_STORAGE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_STORAGE_JSON_BYTES = 64 * 1024
MAX_STATUS_RECORD_BYTES = 64 * 1024
MAX_CATALOG_REQUEST_BYTES = 2 * 1024 * 1024
_PRIVATE_PROVIDER_KEYS = frozenset(
    {
        "accesstoken",
        "address",
        "apnstokens",
        "email",
        "fcmtokens",
        "iccid",
        "password",
        "paymentmethodid",
        "phone",
        "refreshtoken",
        "simiccid",
        "stripecustomerid",
    }
)
_STATUS_OBJECT_NAME = re.compile(r"\A\d{8}T\d{6}Z-[0-9a-f]{32}\.json\Z")


def _postgrest_auth_headers(api_key: str) -> Dict[str, str]:
    """Modern Supabase keys are API keys, not bearer JWTs."""
    headers = {"apikey": api_key}
    if not api_key.startswith(("sb_secret_", "sb_publishable_")):
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class StorageError(RuntimeError):
    """A Supabase Storage operation failed."""

    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        provider_code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.provider_code = provider_code


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
        self.failure_stages: Dict[str, int] = {}
        self.failure_hosts: Dict[str, int] = {}
        self._catalog_enabled = False

    def set_catalog_enabled(self, enabled: bool) -> None:
        """Opt into the relational catalog after its migration has been applied."""
        self._catalog_enabled = bool(enabled)

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
        self.failure_stages = {}
        self.failure_hosts = {}
        catalog_items = []
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
                            catalog_items.append(
                                _catalog_item(photo, image_path, image_body, marker)
                            )
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
                    catalog_items.append(
                        _catalog_item(photo, image_path, stored_image, stored_marker)
                    )
                except StorageError:
                    self.progress_failed += 1
                    raise
                except (KeyError, OSError, RuntimeError, ValueError, TypeError) as exc:
                    self.progress_failed += 1
                    stage = _photo_failure_stage(exc)
                    self.failure_stages[stage] = self.failure_stages.get(stage, 0) + 1
                    if stage == "image_host":
                        hostname = urlsplit(str(photo.get("photoUrl") or "")).hostname
                        if (
                            hostname
                            and len(self.failure_hosts) < 10
                            and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", hostname)
                        ):
                            self.failure_hosts[hostname] = self.failure_hosts.get(hostname, 0) + 1

            if len(photos) < page_size:
                break
            page += 1
        if self._catalog_enabled:
            cameras = []
            try:
                check_deadline()
                cameras = client.get_cameras()
            except Exception:  # Catalog is auxiliary; never mask verified archive success.
                self.progress_failed += 1
                self.failure_stages["catalog_cameras"] = (
                    self.failure_stages.get("catalog_cameras", 0) + 1
                )
            try:
                check_deadline()
                self._write_catalog(cameras, catalog_items)
            except Exception:  # Catalog is auxiliary; never mask verified archive success.
                self.progress_failed += 1
                self.failure_stages["catalog_index"] = (
                    self.failure_stages.get("catalog_index", 0) + 1
                )
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

    def read_private_image(self, object_path: str, *, max_bytes: int) -> bytes:
        """Read one bounded object while preserving private-bucket access."""
        self._ensure_bucket()
        body = self._download(object_path, max_bytes=max_bytes)
        if body is None:
            raise StorageError("Preview is unavailable")
        return body

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

    def _write_catalog(self, cameras: Any, media: Any) -> None:
        """Atomically index verified archive units through the private RPC."""
        observed_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        safe_cameras = [
            _sanitize_provider_payload(camera)
            for camera in cameras
            if isinstance(camera, dict)
        ]
        body = json.dumps(
            {
                "p_cameras": safe_cameras,
                "p_media": media,
                "p_observed_at": observed_at,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_CATALOG_REQUEST_BYTES:
            raise StorageError("Supabase catalog request exceeds size limit")
        response = self._transport.request(
            "POST",
            f"{self.base_url}/rest/v1/rpc/deerid_ingest_reveal_batch",
            headers={
                **_postgrest_auth_headers(self._headers["apikey"]),
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            body=body,
            max_response_bytes=MAX_STORAGE_JSON_BYTES,
        )
        if response.status not in {200, 204}:
            raise StorageError(
                f"Supabase catalog write failed with HTTP {response.status}",
                http_status=response.status,
                provider_code=_safe_provider_code(response),
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
        raise StorageError(
            f"Supabase object read failed with HTTP {response.status}",
            http_status=response.status,
            provider_code=_safe_provider_code(response),
        )

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
        encoded = quote(f"{self.bucket}/{object_path}", safe="/@")
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
    code = str(payload.get("code", "")).lower()
    message = str(payload.get("message", "")).lower()
    return (
        status_code == "404"
        or error in {"not_found", "object_not_found"}
        or code in {"nosuchkey", "not_found", "object_not_found"}
        or "not found" in message
    )


def _safe_provider_code(response: StorageResponse) -> Optional[str]:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for field in ("code", "error", "statusCode"):
        candidate = str(payload.get(field, "")).strip().replace(" ", "_")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", candidate):
            return candidate
    return None


def _catalog_item(
    photo: Dict[str, Any],
    object_path: str,
    image: Optional[bytes],
    marker: Optional[bytes],
) -> Dict[str, Any]:
    """Build a bounded catalog record only from a verified archive unit."""
    if image is None or marker is None:
        raise ValueError("verified catalog item is incomplete")
    digest = hashlib.sha256(image).hexdigest()
    try:
        marker_digest = marker.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("verified catalog checksum is invalid") from exc
    if not hmac.compare_digest(digest, marker_digest):
        raise ValueError("verified catalog checksum does not match")
    dimensions = _image_dimensions(image)
    item: Dict[str, Any] = {
        "provider": _sanitize_provider_payload(photo),
        "object_path": object_path,
        "image_sha256": digest,
        "image_bytes": len(image),
        "content_type": _content_type(object_path),
    }
    if dimensions is not None:
        item["width"], item["height"] = dimensions
    return item


def _sanitize_provider_payload(value: Any) -> Any:
    """Keep provider evidence while excluding credentials, PII, and signed URLs."""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if (
                normalized in _PRIVATE_PROVIDER_KEYS
                or normalized.endswith("token")
                or normalized.endswith("tokens")
                or normalized.endswith("url")
            ):
                continue
            cleaned[key] = _sanitize_provider_payload(child)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_provider_payload(item) for item in value]
    return value


def _image_dimensions(body: bytes) -> Optional[tuple[int, int]]:
    """Read PNG/JPEG dimensions without decoding or adding a dependency."""
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 24:
        width = int.from_bytes(body[16:20], "big")
        height = int.from_bytes(body[20:24], "big")
        return (width, height) if width > 0 and height > 0 else None
    if not body.startswith(b"\xff\xd8"):
        return None
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(body):
        if body[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(body) and body[offset] == 0xFF:
            offset += 1
        if offset >= len(body):
            return None
        marker_code = body[offset]
        offset += 1
        if marker_code in {0xD8, 0xD9}:
            continue
        if marker_code == 0xDA:
            return None
        if offset + 2 > len(body):
            return None
        length = int.from_bytes(body[offset:offset + 2], "big")
        if length < 2 or offset + length > len(body):
            return None
        if marker_code in sof_markers and length >= 7:
            height = int.from_bytes(body[offset + 3:offset + 5], "big")
            width = int.from_bytes(body[offset + 5:offset + 7], "big")
            return (width, height) if width > 0 and height > 0 else None
        offset += length
    return None


def _photo_failure_stage(exc: BaseException) -> str:
    message = str(exc).lower()
    if any(field in message for field in ("cameraid", "photoid", "photodateutc", "stable")):
        return "photo_metadata"
    if "trusted" in message:
        return "image_host"
    if any(term in message for term in ("photourl", "photo url", "image download")):
        return "image_download"
    if any(term in message for term in ("jpeg", "png", "image type")):
        return "image_content"
    if "read-back" in message or "verification" in message:
        return "storage_verification"
    return "photo_processing"


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
        and _metadata_correlates(stored_metadata, photo)
        and hmac.compare_digest(expected, actual)
    )


def _check_deadline(deadline: Optional[float], clock: Callable[[], float]) -> None:
    if deadline is not None and deadline - clock() < MIN_REQUEST_BUDGET:
        raise StorageError("Vercel sync deadline reached")
