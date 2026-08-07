import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from reveal_downloader.archive import PhotoArchive


class FakeClient:
    def __init__(self, photos):
        self.photos = photos
        self.download_calls = []
        self.photo_calls = []

    def get_photos(self, *, size, page, camera_id=None):
        self.photo_calls.append(page)
        return self.photos if page == 0 else []

    def download(self, url):
        self.download_calls.append(url)
        return b"jpeg-bytes"


class RepeatingPageClient(FakeClient):
    def get_photos(self, *, size, page, camera_id=None):
        self.photo_calls.append(page)
        return self.photos


class PhotoArchiveTests(unittest.TestCase):
    def test_sync_archives_photo_and_metadata(self):
        photo = {
            "photoId": "photo-1",
            "cameraId": "cam-1",
            "cameraName": "North Blind",
            "photoDateUtc": "2026-08-06T12:34:56.000Z",
            "filename": "IMG_0001.JPG",
            "photoUrl": "https://example.test/photo.jpg",
            "metadata": {"batteryLevel": "93"},
        }
        client = FakeClient([photo])

        with tempfile.TemporaryDirectory() as tmp:
            archive = PhotoArchive(Path(tmp))
            result = archive.sync(client)

            self.assertEqual(result.downloaded, 1)
            image = Path(tmp) / "cam-1" / "2026" / "08" / "06" / "20260806T123456Z_photo-1.jpg"
            metadata = image.with_suffix(".json")
            self.assertEqual(image.read_bytes(), b"jpeg-bytes")
            self.assertEqual(json.loads(metadata.read_text())["photoId"], "photo-1")
            self.assertEqual(
                image.with_suffix(".sha256").read_text().strip(),
                hashlib.sha256(b"jpeg-bytes").hexdigest(),
            )

    def test_sync_skips_a_photo_already_in_the_archive(self):
        photo = {
            "photoId": "photo-1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:34:56Z",
            "filename": "IMG_0001.JPG",
            "photoUrl": "https://example.test/photo.jpg",
        }
        client = FakeClient([photo])

        with tempfile.TemporaryDirectory() as tmp:
            archive = PhotoArchive(Path(tmp))
            archive.sync(client)
            result = archive.sync(client)

            self.assertEqual(result.downloaded, 0)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(len(client.download_calls), 1)

    def test_sync_records_bad_photo_and_continues(self):
        bad_photo = {
            "photoId": "bad",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:34:56Z",
        }
        good_photo = {
            "photoId": "good",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:35:56Z",
            "photoUrl": "https://example.test/good.jpg",
        }
        client = FakeClient([bad_photo, good_photo])

        with tempfile.TemporaryDirectory() as tmp:
            result = PhotoArchive(Path(tmp)).sync(client)

            self.assertEqual(result.failed, 1)
            self.assertEqual(result.downloaded, 1)

    def test_sync_creates_private_archive_directories(self):
        photo = {
            "photoId": "p1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p1.jpg",
        }
        client = FakeClient([photo])

        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp) / "archive"
            PhotoArchive(archive_root).sync(client)

            self.assertEqual(archive_root.stat().st_mode & 0o777, 0o700)

    def test_sync_stops_when_api_repeats_a_full_page(self):
        photo = {
            "photoId": "p1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p1.jpg",
        }
        client = RepeatingPageClient([photo])

        with tempfile.TemporaryDirectory() as tmp:
            PhotoArchive(Path(tmp)).sync(client, page_size=1, max_pages=3)

        self.assertEqual(client.photo_calls, [0, 1])


if __name__ == "__main__":
    unittest.main()
