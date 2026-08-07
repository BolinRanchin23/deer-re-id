import json
import unittest

from reveal_downloader.supabase import StorageResponse, SupabaseArchive


class FakeStorageTransport:
    def __init__(self, existing=None):
        self.objects = dict(existing or {})
        self.calls = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, headers or {}, body))
        if "/storage/v1/bucket/" in url:
            return StorageResponse(200, b"{}")
        marker = "/storage/v1/object/info/"
        if marker in url:
            object_path = url.split(marker, 1)[1]
            return StorageResponse(200 if object_path in self.objects else 404, b"{}")
        marker = "/storage/v1/object/"
        if marker in url and method == "POST":
            object_path = url.split(marker, 1)[1]
            self.objects[object_path] = body
            return StorageResponse(200, b"{}")
        return StorageResponse(404, b"{}")


class FakeRevealClient:
    def __init__(self):
        self.download_calls = []

    def get_photos(self, *, size, page, camera_id=None):
        if page:
            return []
        return [{
            "photoId": "p1",
            "cameraId": "cam-1",
            "photoDateUtc": "2026-08-06T12:00:00Z",
            "photoUrl": "https://example.test/p1.jpg",
        }]

    def download(self, url):
        self.download_calls.append(url)
        return b"jpeg"


class SupabaseArchiveTests(unittest.TestCase):
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
        self.assertTrue(uploaded[0].startswith("tactacam-photos/cam-1/2026/08/06/"))
        metadata_key = next(key for key in uploaded if key.endswith(".json"))
        self.assertEqual(json.loads(transport.objects[metadata_key])["photoId"], "p1")
        self.assertTrue(all("/rest/v1/" not in call[1] for call in transport.calls))

    def test_sync_skips_photo_when_checksum_marker_exists(self):
        marker = "tactacam-photos/cam-1/2026/08/06/20260806T120000Z_p1.sha256"
        transport = FakeStorageTransport({marker: b"checksum"})
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


if __name__ == "__main__":
    unittest.main()
