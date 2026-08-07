"""Durable, duplicate-safe local photo archive."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class SyncResult:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


class PhotoArchive:
    """Download Reveal photos into a camera/date directory tree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if not self.root.exists():
            self.root.mkdir(parents=True, mode=0o700)
            self.root.chmod(0o700)

    def sync(
        self,
        client: Any,
        *,
        camera_id: Optional[str] = None,
        page_size: int = 100,
        max_pages: int = 0,
    ) -> SyncResult:
        downloaded = 0
        skipped = 0
        failed = 0
        page = 0
        seen_pages = set()
        while max_pages <= 0 or page < max_pages:
            photos = client.get_photos(size=page_size, page=page, camera_id=camera_id)
            if not photos:
                break
            page_fingerprint = tuple(
                str(photo.get("photoId") or photo.get("photoUrl") or photo.get("filename"))
                for photo in photos
            )
            if page_fingerprint in seen_pages:
                break
            seen_pages.add(page_fingerprint)
            for photo in photos:
                image_path = self._image_path(photo)
                metadata_path = image_path.with_suffix(".json")
                checksum_path = image_path.with_suffix(".sha256")
                if image_path.exists() and metadata_path.exists() and checksum_path.exists():
                    skipped += 1
                    continue
                try:
                    photo_url = photo.get("photoUrl")
                    if not photo_url:
                        raise ValueError("photo has no photoUrl")
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_bytes = client.download(photo_url)
                    image_tmp = image_path.with_suffix(image_path.suffix + ".part")
                    metadata_tmp = metadata_path.with_suffix(".json.part")
                    checksum_tmp = checksum_path.with_suffix(".sha256.part")
                    image_tmp.write_bytes(image_bytes)
                    metadata_tmp.write_text(
                        json.dumps(photo, indent=2, sort_keys=True), encoding="utf-8"
                    )
                    checksum_tmp.write_text(
                        hashlib.sha256(image_bytes).hexdigest() + "\n", encoding="ascii"
                    )
                    image_tmp.replace(image_path)
                    metadata_tmp.replace(metadata_path)
                    checksum_tmp.replace(checksum_path)
                    downloaded += 1
                except (KeyError, OSError, RuntimeError, ValueError, TypeError):
                    failed += 1
            if len(photos) < page_size:
                break
            page += 1
        return SyncResult(downloaded=downloaded, skipped=skipped, failed=failed)

    def _image_path(self, photo: Dict[str, Any]) -> Path:
        return self.root / relative_photo_path(photo)


def relative_photo_path(photo: Dict[str, Any]) -> Path:
    """Return the stable camera/date/image path shared by local and cloud archives."""
    camera_id = _safe_component(str(photo.get("cameraId") or "unknown-camera"))
    captured = _parse_date(photo.get("photoDateUtc"))
    photo_id = _safe_component(str(photo.get("photoId") or photo.get("filename") or "photo"))
    extension = _extension(photo)
    timestamp = captured.strftime("%Y%m%dT%H%M%SZ")
    return (
        Path(camera_id)
        / captured.strftime("%Y")
        / captured.strftime("%m")
        / captured.strftime("%d")
        / f"{timestamp}_{photo_id}{extension}"
    )


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "unknown"


def _parse_date(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _extension(photo: Dict[str, Any]) -> str:
    filename = str(photo.get("filename") or "")
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    path_suffix = Path(urlparse(str(photo.get("photoUrl") or "")).path).suffix.lower()
    return path_suffix if path_suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
