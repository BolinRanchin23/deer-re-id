import hashlib
import json
import time
import unittest
from unittest.mock import patch

from reveal_downloader.archive import relative_photo_path
from reveal_downloader.supabase import (
    StorageError,
    StorageResponse,
    StorageTransport,
    SupabaseArchive,
)


class FakeStorageTransport:
    def __init__(self, existing=None, bucket_body=b'{"public":false}'):
        self.objects = dict(existing or {})
        self.calls = []
        self.bucket_body = bucket_body

    def set_deadline(self, deadline, clock=None):
        self.deadline = deadline
        self.clock = clock

    def request(self, method, url, *, headers=None, body=None, max_response_bytes=None):
        self.calls.append((method, url, headers or {}, body))
        if "/storage/v1/bucket/" in url:
            return StorageResponse(200, self.bucket_body)
        marker = "/storage/v1/object/list/"
        if marker in url and method == "POST":
            bucket = url.split(marker, 1)[1]
            options = json.loads(body)
            prefix = options["prefix"].strip("/") + "/"
            names = [
                key[len(bucket) + len(prefix) + 1:]
                for key in self.objects
                if key.startswith(f"{bucket}/{prefix}")
            ]
            names.sort(reverse=True)
            return StorageResponse(200, json.dumps([{"name": name} for name in names[:options["limit"]]]).encode())
        marker = "/storage/v1/object/authenticated/"
        if marker in url and method == "GET":
            object_path = url.split(marker, 1)[1]
            if object_path in self.objects:
                if max_response_bytes is not None and len(self.objects[object_path]) > max_response_bytes:
                    raise StorageError("response exceeds size limit")
                return StorageResponse(200, self.objects[object_path])
            return StorageResponse(404, b"{}")
        marker = "/storage/v1/object/"
        if marker in url and method == "POST":
            object_path = url.split(marker, 1)[1]
            self.objects[object_path] = body
            return StorageResponse(200, b"{}")
        if marker in url and method == "DELETE":
            object_path = url.split(marker, 1)[1]
            self.objects.pop(object_path, None)
            return StorageResponse(200, b"{}")
        return StorageResponse(404, b"{}")


class FakeRevealClient:
    def __init__(self, *, photo=None, image=b"\xff\xd8jpeg\xff\xd9"):
        self.download_calls = []
        self.photo = photo or {
            "photoId": "p1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p1.jpg",
        }
        self.image = image

    def set_deadline(self, deadline, clock=None):
        self.deadline = deadline
        self.clock = clock

    def get_photos(self, *, size, page, camera_id=None):
        if page:
            return []
        return [self.photo]

    def download(self, url):
        self.download_calls.append(url)
        return self.image


class SupabaseArchiveTests(unittest.TestCase):
    def test_individual_photo_failures_are_safely_categorized(self):
        class RejectedImageClient(FakeRevealClient):
            def download(self, url):
                raise ValueError("photo URL host is not trusted")

        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=FakeStorageTransport(),
        )

        result = archive.sync(RejectedImageClient(), max_pages=1)

        self.assertEqual(result.failed, 1)
        self.assertEqual(archive.failure_stages, {"image_host": 1})
        self.assertEqual(archive.failure_hosts, {"example.test": 1})

    def test_missing_object_http_400_is_treated_as_absent(self):
        class SupabaseMissingObjectTransport(FakeStorageTransport):
            def request(self, method, url, *, headers=None, body=None, max_response_bytes=None):
                marker = "/storage/v1/object/authenticated/"
                if marker in url and method == "GET":
                    object_path = url.split(marker, 1)[1]
                    if object_path not in self.objects:
                        self.calls.append((method, url, headers or {}, body))
                        return StorageResponse(
                            400,
                            b'{"statusCode":"404","error":"not_found","message":"Object not found"}',
                        )
                return super().request(
                    method,
                    url,
                    headers=headers,
                    body=body,
                    max_response_bytes=max_response_bytes,
                )

        transport = SupabaseMissingObjectTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )

        result = archive.sync(FakeRevealClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 0)

    def test_missing_resource_http_400_variant_is_treated_as_absent(self):
        class MissingResourceTransport(FakeStorageTransport):
            def request(self, method, url, *, headers=None, body=None, max_response_bytes=None):
                marker = "/storage/v1/object/authenticated/"
                if marker in url and method == "GET":
                    object_path = url.split(marker, 1)[1]
                    if object_path not in self.objects:
                        self.calls.append((method, url, headers or {}, body))
                        return StorageResponse(
                            400,
                            b'{"statusCode":"400","error":"Bad Request","code":"NoSuchKey","message":"The resource was not found"}',
                        )
                return super().request(
                    method,
                    url,
                    headers=headers,
                    body=body,
                    max_response_bytes=max_response_bytes,
                )

        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=MissingResourceTransport(),
        )

        result = archive.sync(FakeRevealClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 0)

    def test_bucket_not_found_http_400_creates_private_bucket(self):
        class MissingBucketTransport(FakeStorageTransport):
            def request(self, method, url, *, headers=None, body=None, max_response_bytes=None):
                if "/storage/v1/bucket/" in url and method == "GET":
                    self.calls.append((method, url, headers or {}, body))
                    return StorageResponse(400, b'{"message":"Bucket not found"}')
                if url.endswith("/storage/v1/bucket") and method == "POST":
                    self.calls.append((method, url, headers or {}, body))
                    return StorageResponse(200, b'{}')
                return super().request(
                    method,
                    url,
                    headers=headers,
                    body=body,
                    max_response_bytes=max_response_bytes,
                )

        transport = MissingBucketTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )
        archive.sync(FakeRevealClient(), max_pages=1)

        creates = [call for call in transport.calls if call[0] == "POST" and call[1].endswith("/storage/v1/bucket")]
        self.assertEqual(len(creates), 1)
        self.assertFalse(json.loads(creates[0][3])["public"])

    @staticmethod
    def _photo():
        return {
            "photoId": "p1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p1.jpg",
        }

    def _valid_objects(self, image=b"\xff\xd8jpeg\xff\xd9"):
        photo = self._photo()
        image_key = "tactacam-photos/" + relative_photo_path(photo).as_posix()
        stem = image_key.rsplit(".", 1)[0]
        return photo, image_key, {
            image_key: image,
            stem + ".json": json.dumps(photo, indent=2, sort_keys=True).encode("utf-8"),
            stem + ".sha256": (hashlib.sha256(image).hexdigest() + "\n").encode("ascii"),
        }

    def test_storage_network_call_is_bounded_below_vercel_deadline_margin(self):
        with patch("reveal_downloader.supabase.urlopen") as mocked:
            response = mocked.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"{}"

            StorageTransport().request("GET", "https://project.supabase.co/storage")

        self.assertLessEqual(mocked.call_args.kwargs["timeout"], 8)

    def test_storage_transport_bounds_success_and_error_response_reads(self):
        with patch("reveal_downloader.supabase.urlopen") as mocked:
            response = mocked.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"12345"

            with self.assertRaisesRegex(StorageError, "size"):
                StorageTransport().request(
                    "GET", "https://project.supabase.co/storage", max_response_bytes=4
                )

            response.read.assert_called_once_with(5)

    def test_storage_transport_uses_remaining_deadline_and_refuses_tiny_budget(self):
        now = [10.0]
        transport = StorageTransport(clock=lambda: now[0])
        transport.set_deadline(13.0)
        with patch("reveal_downloader.supabase.urlopen") as mocked:
            response = mocked.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"{}"
            transport.request("GET", "https://project.supabase.co/storage")
            self.assertEqual(mocked.call_args.kwargs["timeout"], 3.0)

            now[0] = 12.95
            with self.assertRaisesRegex(StorageError, "deadline"):
                transport.request("GET", "https://project.supabase.co/storage")

        self.assertEqual(mocked.call_count, 1)

    def test_sync_propagates_one_deadline_to_storage_and_reveal(self):
        transport = FakeStorageTransport()
        client = FakeRevealClient()
        clock = lambda: 10.0
        SupabaseArchive(
            "https://project.supabase.co", "test-key", transport=transport
        ).sync(client, max_pages=1, deadline=20.0, clock=clock)

        self.assertEqual(transport.deadline, 20.0)
        self.assertEqual(client.deadline, 20.0)
        self.assertIs(transport.clock, clock)
        self.assertIs(client.clock, clock)

    def test_sync_stops_before_work_when_deadline_has_expired(self):
        transport = FakeStorageTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=transport,
        )

        with self.assertRaisesRegex(StorageError, "deadline"):
            archive.sync(
                FakeRevealClient(),
                max_pages=1,
                deadline=time.monotonic() - 1,
            )

        self.assertEqual(transport.calls, [])

    def test_sync_starts_no_request_when_remaining_budget_is_too_small(self):
        transport = FakeStorageTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )

        with self.assertRaisesRegex(StorageError, "deadline"):
            archive.sync(
                FakeRevealClient(), deadline=10.05, clock=lambda: 10.0
            )

        self.assertEqual(transport.calls, [])

    def test_sync_rechecks_deadline_between_remote_operations(self):
        transport = FakeStorageTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=transport,
        )
        times = iter((0.0, 100.0))

        with self.assertRaisesRegex(StorageError, "deadline"):
            archive.sync(
                FakeRevealClient(),
                max_pages=1,
                deadline=50.0,
                clock=lambda: next(times),
            )

        self.assertEqual(len(transport.calls), 1)

    def test_deadline_expiration_inside_photo_is_not_swallowed(self):
        transport = FakeStorageTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=transport,
        )
        times = iter((0.0, 0.0, 0.0, 0.0, 100.0))

        with self.assertRaisesRegex(StorageError, "deadline"):
            archive.sync(
                FakeRevealClient(),
                max_pages=1,
                deadline=50.0,
                clock=lambda: next(times),
            )

    def test_rejects_plain_http_project_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            SupabaseArchive("http://project.supabase.co", "sb_secret_test")

    def test_existing_bucket_must_explicitly_be_private(self):
        for body in (b'{"public":true}', b"{}", b"not-json", b'[{"public":false}]'):
            with self.subTest(body=body):
                archive = SupabaseArchive(
                    "https://project.supabase.co",
                    "test-key",
                    transport=FakeStorageTransport(bucket_body=body),
                )
                with self.assertRaisesRegex(StorageError, "private"):
                    archive.read_dashboard_runs()

    def test_modern_secret_key_sends_required_storage_authorization(self):
        transport = FakeStorageTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=transport,
        )

        archive.sync(FakeRevealClient(), max_pages=1)

        self.assertTrue(transport.calls)
        for _, _, headers, _ in transport.calls:
            self.assertEqual(headers.get("apikey"), "sb_secret_test")
            self.assertEqual(headers.get("Authorization"), "Bearer sb_secret_test")

    def test_sync_rejects_image_magic_that_disagrees_with_object_extension(self):
        cases = (
            ({**self._photo(), "filename": "photo.png"}, b"\xff\xd8jpeg\xff\xd9"),
            ({**self._photo(), "filename": "photo.jpg"}, b"\x89PNG\r\n\x1a\npng"),
            ({**self._photo(), "filename": ""}, b"\x89PNG\r\n\x1a\npng"),
        )
        for photo, image in cases:
            with self.subTest(filename=photo["filename"]):
                transport = FakeStorageTransport()
                result = SupabaseArchive(
                    "https://project.supabase.co", "test-key", transport=transport
                ).sync(FakeRevealClient(photo=photo, image=image), max_pages=1)

                self.assertEqual(result.failed, 1)
                self.assertEqual(result.downloaded, 0)
                self.assertEqual(transport.objects, {})

    def test_new_upload_is_not_counted_until_all_objects_read_back_and_verify(self):
        class CorruptingTransport(FakeStorageTransport):
            def request(self, method, url, **kwargs):
                response = super().request(method, url, **kwargs)
                if method == "POST" and url.endswith(".sha256"):
                    image_key = next(key for key in self.objects if key.endswith(".jpg"))
                    self.objects[image_key] = b"corrupt-after-upload"
                return response

        transport = CorruptingTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co", "test-key", transport=transport
        )

        result = archive.sync(FakeRevealClient(), max_pages=1)

        self.assertEqual(result.downloaded, 0)
        self.assertEqual(result.failed, 1)
        self.assertEqual(archive.last_archive_units, [])
        self.assertFalse(any(key.endswith(".sha256") for key in transport.objects))

    def test_sync_exposes_committed_progress_when_later_storage_call_aborts(self):
        first = self._photo()
        second = {**first, "photoId": "p2", "photoUrl": "https://example.test/p2.jpg"}

        class TwoPhotoClient(FakeRevealClient):
            def get_photos(self, *, size, page, camera_id=None):
                return [first, second] if page == 0 else []

        second_marker = relative_photo_path(second).with_suffix(".sha256").as_posix()

        class AbortingTransport(FakeStorageTransport):
            def request(self, method, url, **kwargs):
                if method == "GET" and url.endswith(second_marker):
                    raise StorageError("storage interrupted")
                return super().request(method, url, **kwargs)

        archive = SupabaseArchive(
            "https://project.supabase.co", "test-key", transport=AbortingTransport()
        )

        with self.assertRaisesRegex(StorageError, "interrupted"):
            archive.sync(TwoPhotoClient(), max_pages=1)

        self.assertEqual(archive.progress_downloaded, 1)
        self.assertEqual(archive.progress_skipped, 0)
        self.assertEqual(archive.progress_failed, 1)
        self.assertEqual(len(archive.last_archive_units), 1)

    def test_sync_uploads_image_metadata_and_checksum(self):
        transport = FakeStorageTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co/rest/v1/",
            "test-key",
            "tactacam-photos",
            transport=transport,
        )
        client = FakeRevealClient()

        result = archive.sync(client)

        self.assertEqual(result.downloaded, 1)
        uploaded = sorted(transport.objects)
        self.assertEqual(len(uploaded), 3)
        self.assertTrue(uploaded[0].startswith("tactacam-photos/cam-1@"))
        metadata_key = next(key for key in uploaded if key.endswith(".json"))
        self.assertEqual(json.loads(transport.objects[metadata_key])["photoId"], "p1")
        self.assertTrue(all("/rest/v1/" not in call[1] for call in transport.calls))
        expected_image_path = relative_photo_path(client.photo).as_posix()
        self.assertEqual(len(archive.last_archive_units), 1)
        self.assertEqual(archive.last_archive_units[0]["object_path"], expected_image_path)
        self.assertEqual(archive.last_archive_units[0]["captured_at"], "2026-08-06T12:00:00Z")

    def test_sync_skips_photo_when_checksum_marker_exists(self):
        _, _, objects = self._valid_objects()
        transport = FakeStorageTransport(objects)
        archive = SupabaseArchive(
            "https://project.supabase.co",
            "test-key",
            "tactacam-photos",
            transport=transport,
        )
        client = FakeRevealClient()

        result = archive.sync(client)

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.downloaded, 0)
        self.assertEqual(client.download_calls, [])

    def test_sync_repairs_cloud_entry_when_only_checksum_marker_exists(self):
        _, image_key, objects = self._valid_objects()
        marker = image_key.rsplit(".", 1)[0] + ".sha256"
        transport = FakeStorageTransport({marker: objects[marker]})
        archive = SupabaseArchive(
            "https://project.supabase.co",
            "test-key",
            "tactacam-photos",
            transport=transport,
        )

        result = archive.sync(FakeRevealClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.skipped, 0)
        self.assertIn(image_key, transport.objects)
        self.assertIn(marker.replace(".sha256", ".json"), transport.objects)

    def test_sync_validates_cloud_marker_metadata_and_image_hash(self):
        photo, image_key, valid = self._valid_objects()
        marker = image_key.rsplit(".", 1)[0] + ".sha256"
        metadata = marker.replace(".sha256", ".json")
        corruptions = (
            {**valid, marker: b"not-a-checksum\n"},
            {**valid, metadata: json.dumps({**photo, "photoId": "other"}).encode()},
            {**valid, image_key: b"different-image"},
        )
        for existing in corruptions:
            with self.subTest(existing=existing):
                transport = FakeStorageTransport(existing)
                archive = SupabaseArchive(
                    "https://project.supabase.co", "test-key", transport=transport
                )

                result = archive.sync(FakeRevealClient(), max_pages=1)

                self.assertEqual(result.downloaded, 1)
                self.assertEqual(result.skipped, 0)
                methods = [call[0] for call in transport.calls]
                self.assertIn("DELETE", methods)
                object_writes = [
                    call for call in transport.calls if call[0] in {"POST", "DELETE"}
                ]
                self.assertEqual(object_writes[-1][0], "POST")
                self.assertTrue(object_writes[-1][1].endswith(".sha256"))

    def test_sync_repairs_cloud_image_whose_magic_disagrees_with_path(self):
        _, image_key, existing = self._valid_objects()
        marker = image_key.rsplit(".", 1)[0] + ".sha256"
        png = b"\x89PNG\r\n\x1a\ncloud-png"
        existing[image_key] = png
        existing[marker] = (hashlib.sha256(png).hexdigest() + "\n").encode("ascii")
        transport = FakeStorageTransport(existing)

        result = SupabaseArchive(
            "https://project.supabase.co", "test-key", transport=transport
        ).sync(FakeRevealClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.skipped, 0)
        self.assertTrue(transport.objects[image_key].startswith(b"\xff\xd8"))

    def test_interrupted_cloud_repair_removes_marker_before_download(self):
        _, image_key, valid = self._valid_objects()
        marker = image_key.rsplit(".", 1)[0] + ".sha256"
        valid[marker] = b"invalid\n"
        transport = FakeStorageTransport(valid)

        class InterruptedReveal(FakeRevealClient):
            def download(self, url):
                self.marker_was_removed = marker not in transport.objects
                raise RuntimeError("interrupted")

        client = InterruptedReveal()
        result = SupabaseArchive(
            "https://project.supabase.co", "test-key", transport=transport
        ).sync(client, max_pages=1)

        self.assertEqual(result.failed, 1)
        self.assertTrue(client.marker_was_removed)
        self.assertNotIn(marker, transport.objects)

    def test_private_object_reads_and_deletes_send_auth_headers(self):
        _, image_key, valid = self._valid_objects()
        marker = image_key.rsplit(".", 1)[0] + ".sha256"
        valid[marker] = b"invalid\n"
        transport = FakeStorageTransport(valid)
        SupabaseArchive(
            "https://project.supabase.co", "legacy.jwt.key", transport=transport
        ).sync(FakeRevealClient(), max_pages=1)

        private_calls = [
            call
            for call in transport.calls
            if call[0] in {"GET", "DELETE"}
            and "/storage/v1/object/" in call[1]
        ]
        self.assertTrue(any("/object/authenticated/" in call[1] for call in private_calls))
        self.assertTrue(any(call[0] == "DELETE" for call in private_calls))
        for _, _, headers, _ in private_calls:
            self.assertEqual(headers["apikey"], "legacy.jwt.key")
            self.assertEqual(headers["Authorization"], "Bearer legacy.jwt.key")
    def test_dashboard_runs_are_immutable_private_objects_listed_newest_first(self):
        transport = FakeStorageTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co", "test-key", transport=transport
        )
        older = {"version": 1, "id": "a" * 32, "finished_at": "2026-08-07T10:00:00Z"}
        newer = {"version": 1, "id": "b" * 32, "finished_at": "2026-08-07T11:00:00Z"}

        self.assertEqual(archive.read_dashboard_runs(), [])
        archive.write_dashboard_run(older)
        archive.write_dashboard_run(newer)

        self.assertEqual(archive.read_dashboard_runs(), [newer, older])
        status_keys = [key for key in transport.objects if key.startswith("tactacam-photos/_status/runs/")]
        self.assertEqual(len(status_keys), 2)
        writes = [
            call
            for call in transport.calls
            if call[0] == "POST" and "/storage/v1/object/" in call[1]
            and "/storage/v1/object/list/" not in call[1]
        ]
        self.assertEqual(writes[-1][2]["Content-Type"], "application/json")
        self.assertEqual(writes[-1][2]["x-upsert"], "false")

    def test_oversized_dashboard_run_fails_closed(self):
        object_path = "tactacam-photos/_status/runs/20260807T100000Z-" + "a" * 32 + ".json"
        transport = FakeStorageTransport({object_path: b"x" * (64 * 1024 + 1)})
        archive = SupabaseArchive(
            "https://project.supabase.co", "test-key", transport=transport
        )

        with self.assertRaisesRegex(StorageError, "size"):
            archive.read_dashboard_runs()

if __name__ == "__main__":
    unittest.main()
