"""Supabase Storage archive used by the Vercel cron function."""

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .archive import SyncResult, relative_photo_path


class StorageError(RuntimeError):
    """A Supabase Storage operation failed."""


@dataclass(frozen=True)
class StorageResponse:
    status: int
    body: bytes


class StorageTransport:
    """Minimal HTTP transport for Supabase Storage."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
    ) -> StorageResponse:
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return StorageResponse(response.status, response.read())
        except HTTPError as exc:
            return StorageResponse(exc.code, exc.read())
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
            "Authorization": f"Bearer {secret_key}",
            "apikey": secret_key,
        }
        self._bucket_ready = False

    def sync(
        self,
        client: Any,
        *,
        camera_id: Optional[str] = None,
        page_size: int = 100,
        max_pages: int = 2,
    ) -> SyncResult:
        self._ensure_bucket()
        downloaded = skipped = failed = 0
        page = 0
        seen_pages = set()
        while max_pages <= 0 or page < max_pages:
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
                image_path = relative_photo_path(photo).as_posix()
                checksum_path = _replace_suffix(image_path, ".sha256")
                if self._exists(checksum_path):
                    skipped += 1
                    continue
                try:
                    photo_url = photo.get("photoUrl")
                    if not photo_url:
                        raise ValueError("photo has no photoUrl")
                    image = client.download(photo_url)
                    metadata = json.dumps(photo, indent=2, sort_keys=True).encode("utf-8")
                    checksum = (hashlib.sha256(image).hexdigest() + "\n").encode("ascii")
                    self._upload(image_path, image, _content_type(image_path))
                    self._upload(_replace_suffix(image_path, ".json"), metadata, "application/json")
                    # The checksum is the completion marker and must be uploaded last.
                    self._upload(checksum_path, checksum, "text/plain")
                    downloaded += 1
                except (KeyError, OSError, RuntimeError, ValueError, TypeError):
                    failed += 1

            if len(photos) < page_size:
                break
            page += 1
        return SyncResult(downloaded=downloaded, skipped=skipped, failed=failed)

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        bucket_url = f"{self.base_url}/storage/v1/bucket/{quote(self.bucket, safe='')}"
        response = self._transport.request("GET", bucket_url, headers=self._headers)
        if response.status == 404:
            body = json.dumps(
                {"id": self.bucket, "name": self.bucket, "public": False}
            ).encode("utf-8")
            headers = {**self._headers, "Content-Type": "application/json"}
            response = self._transport.request(
                "POST",
                f"{self.base_url}/storage/v1/bucket",
                headers=headers,
                body=body,
            )
        if response.status not in {200, 201}:
            raise StorageError(f"Supabase bucket setup failed with HTTP {response.status}")
        self._bucket_ready = True

    def _exists(self, object_path: str) -> bool:
        url = self._object_url("info", object_path)
        response = self._transport.request("GET", url, headers=self._headers)
        if response.status == 200:
            return True
        if response.status == 404:
            return False
        raise StorageError(f"Supabase object check failed with HTTP {response.status}")

    def _upload(self, object_path: str, body: bytes, content_type: str) -> None:
        headers = {
            **self._headers,
            "Content-Type": content_type,
            "x-upsert": "true",
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
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


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
    if lowered.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"
