import unittest

from reveal_downloader.catalog import (
    handle_library,
    handle_library_preview,
)
from reveal_downloader.supabase import _postgrest_auth_headers


class MemoryCatalog:
    def __init__(self):
        self.deadline = None
        self.resolved = None

    def set_deadline(self, deadline, clock=None):
        self.deadline = deadline
        self.clock = clock

    def read_library(self, limit=60):
        return [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "captured_at": "2026-08-08T12:00:00Z",
                "camera_id": "22222222-2222-4222-8222-222222222222",
                "camera_name": "North Ridge",
                "variant": "cloud_thumbnail",
                "width": 1280,
                "height": 720,
                "labels": [{"namespace": "species", "label": "deer", "status": "suggested"}],
                "animals": [],
                "object_path": "must-not-leak.jpg",
            }
        ][:limit]

    def read_camera_map(self):
        return [
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "name": "North Ridge",
                "location_name": "Ridge",
                "latitude": 30.123456,
                "longitude": -100.123456,
                "observed_at": "2026-08-08T12:00:00Z",
                "battery_level": 82,
                "signal_level": 4,
            }
        ]

    def resolve_media_object(self, media_id):
        self.resolved = media_id
        return {
            "object_path": "cam@" + "a" * 64 + "/2026/08/08/20260808T120000Z_photo@" + "b" * 64 + ".jpg",
            "content_type": "image/jpeg",
        }

    def read_private_image(self, object_path, max_bytes):
        self.read_args = (object_path, max_bytes)
        return b"\xff\xd8private\xff\xd9"


class PrivateLibraryTests(unittest.TestCase):
    def test_postgrest_auth_distinguishes_modern_keys_from_legacy_jwts(self):
        self.assertEqual(_postgrest_auth_headers("sb_secret_test"), {"apikey": "sb_secret_test"})
        legacy = _postgrest_auth_headers("legacy.jwt.value")
        self.assertEqual(legacy["apikey"], "legacy.jwt.value")
        self.assertEqual(legacy["Authorization"], "Bearer legacy.jwt.value")

    @staticmethod
    def environment():
        return {
            "LIBRARY_PREVIEW_SECRET": "preview-signing-secret-at-least-16",
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SECRET_KEY": "secret",
            "NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN": "pk.mapbox-browser-token",
        }

    def test_library_fails_closed_without_configured_or_authenticated_user(self):
        self.assertEqual(handle_library({}, False)[0], 404)
        status, payload = handle_library(
            self.environment(),
            False,
            catalog_factory=lambda *_: self.fail("catalog must not be created"),
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"ok": False, "error": "unauthorized"})

    def test_authorized_library_returns_photos_map_and_opaque_preview_urls(self):
        catalog = MemoryCatalog()
        status, payload = handle_library(
            self.environment(),
            True,
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_000,
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["photos"]), 1)
        self.assertEqual(len(payload["cameras"]), 1)
        self.assertEqual(payload["mapbox_access_token"], "pk.mapbox-browser-token")
        self.assertRegex(payload["photos"][0]["preview_url"], r"^/api/library_preview\?token=")
        serialized = str(payload)
        self.assertNotIn("must-not-leak.jpg", serialized)
        self.assertNotIn("SUPABASE_SECRET_KEY", serialized)

    def test_library_preview_resolves_media_server_side_and_rejects_tampering(self):
        catalog = MemoryCatalog()
        _, payload = handle_library(
            self.environment(),
            True,
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_000,
        )
        token = payload["photos"][0]["preview_url"].split("token=", 1)[1]

        status, content_type, body = handle_library_preview(
            self.environment(),
            token,
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_001,
        )

        self.assertEqual((status, content_type), (200, "image/jpeg"))
        self.assertEqual(body, b"\xff\xd8private\xff\xd9")
        self.assertEqual(catalog.resolved, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(
            handle_library_preview(
                self.environment(), token + "x", catalog_factory=lambda *_: catalog,
                epoch_now=1_786_200_001,
            )[0],
            404,
        )


if __name__ == "__main__":
    unittest.main()
