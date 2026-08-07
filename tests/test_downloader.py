import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from reveal_downloader.archive import PhotoArchive, relative_photo_path


class FakeClient:
    def __init__(self, photos, image=b"\xff\xd8jpeg-bytes\xff\xd9"):
        self.photos = photos
        self.image = image
        self.download_calls = []
        self.photo_calls = []

    def get_photos(self, *, size, page, camera_id=None):
        self.photo_calls.append(page)
        return self.photos if page == 0 else []

    def download(self, url):
        self.download_calls.append(url)
        return self.image


class RepeatingPageClient(FakeClient):
    def get_photos(self, *, size, page, camera_id=None):
        self.photo_calls.append(page)
        return self.photos


class PhotoArchiveTests(unittest.TestCase):
    @staticmethod
    def _photo(**overrides):
        photo = {
            "photoId": "p1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p1.jpg",
        }
        photo.update(overrides)
        return photo

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
            image = Path(tmp) / relative_photo_path(photo)
            metadata = image.with_suffix(".json")
            self.assertEqual(image.read_bytes(), b"\xff\xd8jpeg-bytes\xff\xd9")
            self.assertEqual(json.loads(metadata.read_text())["photoId"], "photo-1")
            self.assertEqual(
                image.with_suffix(".sha256").read_text().strip(),
                hashlib.sha256(b"\xff\xd8jpeg-bytes\xff\xd9").hexdigest(),
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

    def test_sync_redownloads_photo_when_checksum_does_not_match(self):
        photo = {
            "photoId": "photo-1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:34:56Z",
            "photoUrl": "https://example.test/photo.jpg",
        }
        client = FakeClient([photo])

        with tempfile.TemporaryDirectory() as tmp:
            archive = PhotoArchive(Path(tmp))
            archive.sync(client)
            image = Path(tmp) / relative_photo_path(photo)
            image.write_bytes(b"corrupt")

            result = archive.sync(client)

            self.assertEqual(result.downloaded, 1)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(image.read_bytes(), b"\xff\xd8jpeg-bytes\xff\xd9")
            self.assertEqual(len(client.download_calls), 2)

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

    def test_sync_counts_invalid_stable_fields_without_aborting_page(self):
        invalid = {
            "photoId": "bad",
            "cameraId": "cam-1",
            "photoDateUtc": "invalid",
            "photoUrl": "https://example.test/bad.jpg",
        }
        good = {
            "photoId": "good",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:35:56Z",
            "photoUrl": "https://example.test/good.jpg",
        }

        with tempfile.TemporaryDirectory() as tmp:
            result = PhotoArchive(Path(tmp)).sync(FakeClient([invalid, good]))

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

    def test_sync_repairs_permissions_on_existing_root_and_generated_entries(self):
        photo = {
            "photoId": "p1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p1.jpg",
        }
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp) / "archive"
            archive_root.mkdir(mode=0o755)
            archive_root.chmod(0o755)

            PhotoArchive(archive_root).sync(FakeClient([photo]))

            self.assertEqual(archive_root.stat().st_mode & 0o777, 0o700)
            for path in archive_root.rglob("*"):
                expected = 0o700 if path.is_dir() else 0o600
                self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))

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

    def test_sanitized_identifiers_produce_collision_resistant_paths(self):
        base = {
            "cameraId": "cam/a",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p.jpg",
        }
        first = relative_photo_path({**base, "photoId": "photo/a"})
        second = relative_photo_path(
            {**base, "cameraId": "cam?a", "photoId": "photo?a"}
        )

        self.assertNotEqual(first, second)

    def test_valid_identifier_cannot_collide_with_encoded_unsafe_identifier(self):
        unsafe = "a/b"
        digest = hashlib.sha256(unsafe.encode("utf-8")).hexdigest()[:12]
        base = self._photo(cameraId="camera")

        unsafe_path = relative_photo_path({**base, "photoId": unsafe})
        formerly_colliding = relative_photo_path(
            {**base, "photoId": f"a_b_{digest}"}
        )

        self.assertNotEqual(unsafe_path, formerly_colliding)

    def test_constructor_rejects_symlinked_archive_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            base = Path(tmp)
            (base / "link").symlink_to(Path(outside), target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                PhotoArchive(base / "link" / "archive")

            self.assertEqual(list(Path(outside).rglob("*")), [])

    def test_sync_rejects_symlinked_directory_without_writing_outside_root(self):
        photo = self._photo()
        client = FakeClient([photo])
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp) / "archive"
            archive = PhotoArchive(root)
            camera_component = relative_photo_path(photo).parts[0]
            (root / camera_component).symlink_to(Path(outside), target_is_directory=True)

            result = archive.sync(client)

            self.assertEqual(result.failed, 1)
            self.assertEqual(list(Path(outside).rglob("*")), [])
            self.assertEqual(client.download_calls, [])

    def test_sync_rejects_any_existing_symlink_under_archive_root(self):
        photo = self._photo()
        client = FakeClient([photo])
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp) / "archive"
            archive = PhotoArchive(root)
            (root / "unrelated-link").symlink_to(Path(outside), target_is_directory=True)

            result = archive.sync(client)

            self.assertEqual(result.failed, 1)
            self.assertEqual(client.download_calls, [])

    def test_sync_repairs_mismatched_metadata_instead_of_skipping(self):
        photo = self._photo()
        client = FakeClient([photo])
        with tempfile.TemporaryDirectory() as tmp:
            archive = PhotoArchive(Path(tmp))
            archive.sync(client)
            image = Path(tmp) / relative_photo_path(photo)
            image.with_suffix(".json").write_text(
                json.dumps({**photo, "photoId": "other"}), encoding="utf-8"
            )

            result = archive.sync(client)

            self.assertEqual(result.downloaded, 1)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(json.loads(image.with_suffix(".json").read_text()), photo)

    def test_interrupted_repair_removes_old_completion_marker_first(self):
        photo = self._photo()
        with tempfile.TemporaryDirectory() as tmp:
            archive = PhotoArchive(Path(tmp))
            archive.sync(FakeClient([photo]))
            marker = (Path(tmp) / relative_photo_path(photo)).with_suffix(".sha256")
            marker.write_text("not-a-valid-marker\n", encoding="ascii")

            class InterruptedClient(FakeClient):
                def download(self, url):
                    self.assert_marker_removed = not marker.exists()
                    raise RuntimeError("interrupted")

            interrupted = InterruptedClient([photo])
            result = archive.sync(interrupted)

            self.assertEqual(result.failed, 1)
            self.assertTrue(interrupted.assert_marker_removed)
            self.assertFalse(marker.exists())

    def test_sync_rejects_image_magic_that_disagrees_with_stored_extension(self):
        cases = (
            (self._photo(filename="photo.png"), b"\xff\xd8jpeg\xff\xd9"),
            (self._photo(filename="photo.jpg"), b"\x89PNG\r\n\x1a\npng"),
            (self._photo(filename=""), b"\x89PNG\r\n\x1a\npng"),
        )
        for photo, image in cases:
            with self.subTest(filename=photo["filename"]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = PhotoArchive(root).sync(FakeClient([photo], image=image))

                self.assertEqual(result.failed, 1)
                self.assertEqual(result.downloaded, 0)
                self.assertEqual([path for path in root.rglob("*") if path.is_file()], [])

    def test_sync_rejects_webp_metadata_deterministically(self):
        photo = self._photo(filename="photo.webp", photoUrl="https://example.test/photo.webp")
        client = FakeClient([photo])

        with tempfile.TemporaryDirectory() as tmp:
            result = PhotoArchive(Path(tmp)).sync(client)

        self.assertEqual(result.failed, 1)
        self.assertEqual(client.download_calls, [])

    def test_photo_path_rejects_missing_or_invalid_stable_fields(self):
        valid = {
            "photoId": "p1",
            "cameraId": "c1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
        }
        invalid_photos = (
            {**valid, "photoId": ""},
            {**valid, "cameraId": None},
            {**valid, "photoDateUtc": "not-a-date"},
            {key: value for key, value in valid.items() if key != "photoDateUtc"},
        )

        for photo in invalid_photos:
            with self.subTest(photo=photo), self.assertRaises(ValueError):
                relative_photo_path(photo)


if __name__ == "__main__":
    unittest.main()
