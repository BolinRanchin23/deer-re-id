"""Durable, duplicate-safe local photo archive."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
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
        requested_root = Path(root).absolute()
        self.root = _canonicalize_archive_path(requested_root)
        _secure_mkdir_tree(self.root)
        _verify_directory(self.root)
        os.chmod(self.root, 0o700, follow_symlinks=False)

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
                try:
                    _reject_any_symlink(self.root)
                    image_path = self._image_path(photo)
                    metadata_path = image_path.with_suffix(".json")
                    checksum_path = image_path.with_suffix(".sha256")
                    _ensure_secure_directory(self.root, image_path.parent)
                    _reject_symlinks(image_path, metadata_path, checksum_path)
                    if _is_complete(image_path, metadata_path, checksum_path, photo):
                        for path in (image_path, metadata_path, checksum_path):
                            os.chmod(path, 0o600, follow_symlinks=False)
                        skipped += 1
                        continue
                    photo_url = photo.get("photoUrl")
                    if not photo_url:
                        raise ValueError("photo has no photoUrl")
                    # A marker is a commit record. Invalidate it before repair work.
                    checksum_path.unlink(missing_ok=True)
                    image_bytes = client.download(photo_url)
                    if detected_image_extension(image_bytes) != image_path.suffix.lower():
                        raise ValueError("downloaded image type does not match archive extension")
                    metadata_bytes = json.dumps(photo, indent=2, sort_keys=True).encode("utf-8")
                    checksum_bytes = (hashlib.sha256(image_bytes).hexdigest() + "\n").encode("ascii")
                    temporary = []
                    try:
                        image_tmp = _write_exclusive_temp(image_path, image_bytes)
                        temporary.append(image_tmp)
                        metadata_tmp = _write_exclusive_temp(metadata_path, metadata_bytes)
                        temporary.append(metadata_tmp)
                        os.replace(image_tmp, image_path)
                        temporary.remove(image_tmp)
                        os.replace(metadata_tmp, metadata_path)
                        temporary.remove(metadata_tmp)
                        # The checksum marker is created and committed only after data.
                        checksum_tmp = _write_exclusive_temp(checksum_path, checksum_bytes)
                        temporary.append(checksum_tmp)
                        os.replace(checksum_tmp, checksum_path)
                        temporary.remove(checksum_tmp)
                    finally:
                        for path in temporary:
                            path.unlink(missing_ok=True)
                    downloaded += 1
                except (KeyError, OSError, RuntimeError, ValueError, TypeError):
                    failed += 1
            if len(photos) < page_size:
                break
            page += 1
        return SyncResult(downloaded=downloaded, skipped=skipped, failed=failed)

    def _image_path(self, photo: Dict[str, Any]) -> Path:
        return self.root / relative_photo_path(photo)


def _canonicalize_archive_path(requested: Path) -> Path:
    """Reject user-controlled ancestor links before choosing the real root."""
    _reject_user_controlled_ancestor_symlinks(requested)
    return requested.resolve(strict=False)


def _secure_mkdir_tree(path: Path) -> None:
    """Create the canonical archive root without following a final symlink."""
    if path.is_symlink():
        raise ValueError("archive root must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _verify_directory(path)


def _reject_user_controlled_ancestor_symlinks(path: Path) -> None:
    """Reject path redirection except immutable, root-owned platform aliases."""
    for candidate in reversed((path, *path.parents)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(metadata.st_mode):
            continue
        try:
            parent_metadata = candidate.parent.stat()
        except OSError as exc:
            raise ValueError("archive path contains an unsafe ancestor symlink") from exc
        trusted_system_alias = (
            metadata.st_uid == 0 and parent_metadata.st_mode & 0o022 == 0
        )
        if not trusted_system_alias:
            raise ValueError("archive path contains a user-controlled ancestor symlink")


def _verify_directory(path: Path) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError("archive path contains a symlink or non-directory")


def _ensure_secure_directory(root: Path, directory: Path) -> None:
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise ValueError("archive path escaped its root") from exc
    current = root
    _verify_directory(current)
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("archive path contains a symlink")
        if not current.exists():
            current.mkdir(mode=0o700)
        _verify_directory(current)
        os.chmod(current, 0o700, follow_symlinks=False)


def _reject_symlinks(*paths: Path) -> None:
    if any(path.is_symlink() for path in paths):
        raise ValueError("archive file must not be a symlink")


def _reject_any_symlink(root: Path) -> None:
    for directory, names, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in names + files:
            if stat.S_ISLNK((parent / name).lstat().st_mode):
                raise ValueError("archive root contains a symlink")


def _write_exclusive_temp(destination: Path, body: bytes) -> Path:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for _ in range(10):
        temporary = destination.parent / (
            f".{destination.name}.{secrets.token_hex(16)}.part"
        )
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            return temporary
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    raise OSError("could not create a secure temporary archive file")


def _is_complete(
    image_path: Path,
    metadata_path: Path,
    checksum_path: Path,
    photo: Dict[str, Any],
) -> bool:
    if not image_path.is_file() or not metadata_path.is_file() or not checksum_path.is_file():
        return False
    try:
        marker = checksum_path.read_bytes()
        if re.fullmatch(rb"[0-9a-f]{64}\n", marker) is None:
            return False
        expected = marker[:-1].decode("ascii")
        image_body = image_path.read_bytes()
        actual = hashlib.sha256(image_body).hexdigest()
        if detected_image_extension(image_body) != image_path.suffix.lower():
            return False
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(metadata, dict)
        and metadata == photo
        and hmac.compare_digest(expected, actual)
    )


def detected_image_extension(body: bytes) -> str:
    """Return the only supported extension after inspecting image magic."""
    if len(body) >= 4 and body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9"):
        return ".jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    raise ValueError("downloaded photo is not a supported JPEG or PNG image")


def relative_photo_path(photo: Dict[str, Any]) -> Path:
    """Return the stable camera/date/image path shared by local and cloud archives."""
    camera_value = photo.get("cameraId")
    photo_value = photo.get("photoId")
    if not isinstance(camera_value, str) or not camera_value.strip():
        raise ValueError("photo has no stable cameraId")
    if not isinstance(photo_value, str) or not photo_value.strip():
        raise ValueError("photo has no stable photoId")
    camera_id = _safe_component(camera_value)
    captured = _parse_date(photo.get("photoDateUtc"))
    photo_id = _safe_component(photo_value)
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
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown"
    cleaned = cleaned[:64]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    # '@' is reserved because sanitization never emits it and Supabase permits it.
    return f"{cleaned}@{digest}"


def _parse_date(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    raise ValueError("photoDateUtc must be a valid timezone-aware timestamp")


def _extension(photo: Dict[str, Any]) -> str:
    filename = str(photo.get("filename") or "")
    suffix = Path(filename).suffix.lower()
    path_suffix = Path(urlparse(str(photo.get("photoUrl") or "")).path).suffix.lower()
    if suffix == ".webp" or path_suffix == ".webp":
        raise ValueError("WebP photos are not supported")
    if suffix in {".jpg", ".jpeg", ".png"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if path_suffix in {".jpg", ".jpeg", ".png"}:
        return ".jpg" if path_suffix == ".jpeg" else path_suffix
    return ".jpg"
