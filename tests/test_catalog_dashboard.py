import unittest
from pathlib import Path

from reveal_downloader.catalog import (
    _sanitize_photos,
    handle_library,
    handle_library_preview,
    handle_review,
)
from reveal_downloader.client import HDRequestRejected, RevealError
from reveal_downloader.supabase import _postgrest_auth_headers


class MemoryCatalog:
    def __init__(self):
        self.deadline = None
        self.resolved = None
        self.reviewed = None
        self.hd_completed = None
        self.hd_failed = None
        self.hd_unknown = None

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
                "gate1": {"id": 17, "review_version": 0, "route": "review", "reason": "target_species", "animal_confidence": 0.98, "species_label": "white-tailed deer", "species_confidence": 0.99, "model_name": "SpeciesNet", "model_version": "4.0.3a"},
                "review_decision": None,
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

    def read_gate1_funnel(self, model_name, model_version):
        return {
            "model_name": model_name,
            "model_version": model_version,
            "total_thumbnails": 100,
            "assessed_thumbnails": 100,
            "pending_thumbnails": 0,
            "review_representatives": 60,
            "event_duplicates": 30,
            "archived": 10,
            "blank_or_below_threshold": 7,
            "confident_non_target": 3,
            "unresolved_review": 59,
            "resolved_review": 1,
        }

    def resolve_media_object(self, media_id):
        self.resolved = media_id
        return {
            "object_path": "cam@" + "a" * 64 + "/2026/08/08/20260808T120000Z_photo@" + "b" * 64 + ".jpg",
            "content_type": "image/jpeg",
        }

    def read_private_image(self, object_path, max_bytes):
        self.read_args = (object_path, max_bytes)
        return b"\xff\xd8private\xff\xd9"

    def record_review(self, media_id, assessment_id, review_version, action, note):
        self.reviewed = (media_id, assessment_id, review_version, action, note)
        return {"ok": True, "media_id": media_id, "action": action}

    def begin_hd_request(self, media_id, assessment_id, review_version, note):
        self.hd_begun = (media_id, assessment_id, review_version, note)
        return {
            "ok": True,
            "should_request": True,
            "request_token": "33333333-3333-4333-8333-333333333333",
            "provider_photo_id": "reveal-photo-1",
            "status": "requesting",
        }

    def complete_hd_request(self, request_token):
        self.hd_completed = request_token
        return {"ok": True, "status": "submitted"}

    def fail_hd_request(self, request_token, error_code):
        self.hd_failed = (request_token, error_code)
        return {"ok": True, "status": "failed"}

    def mark_hd_request_unknown(self, request_token, error_code):
        self.hd_unknown = (request_token, error_code)
        return {"ok": True, "status": "unknown"}


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

    def test_open_prototype_library_still_requires_server_configuration(self):
        self.assertEqual(handle_library({})[0], 404)

    def test_open_prototype_library_returns_photos_map_and_opaque_preview_urls(self):
        catalog = MemoryCatalog()
        status, payload = handle_library(
            self.environment(),
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_000,
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["photos"]), 1)
        self.assertEqual(len(payload["cameras"]), 1)
        self.assertEqual(payload["mapbox_access_token"], "pk.mapbox-browser-token")
        self.assertRegex(payload["photos"][0]["preview_url"], r"^/api/library_preview\?token=")
        self.assertRegex(payload["photos"][0]["review_token"], r"^[0-9]+\.")
        self.assertEqual(payload["photos"][0]["gate1"]["route"], "review")
        self.assertEqual(payload["pipeline"]["total_thumbnails"], 100)
        self.assertEqual(payload["pipeline"]["review_representatives"], 60)
        self.assertEqual(payload["pipeline"]["model_name"], "SpeciesNet")
        serialized = str(payload)
        self.assertNotIn("must-not-leak.jpg", serialized)
        self.assertNotIn("SUPABASE_SECRET_KEY", serialized)

    def test_library_preview_resolves_media_server_side_and_rejects_tampering(self):
        catalog = MemoryCatalog()
        _, payload = handle_library(
            self.environment(),
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

    def test_pending_hd_item_is_not_issued_an_actionable_review_token(self):
        photos = _sanitize_photos(
            [{
                "id": "11111111-1111-4111-8111-111111111111",
                "gate1": {
                    "id": 17, "route": "review", "review_version": 0,
                    "pending_hd": True,
                },
                "review_decision": None,
            }],
            b"0123456789abcdef",
            1_786_200_000,
        )
        self.assertNotIn("review_token", photos[0])
        self.assertTrue(photos[0]["gate1"]["pending_hd"])

    def test_signed_review_token_records_allowed_action_and_rejects_tampering(self):
        catalog = MemoryCatalog()
        _, payload = handle_library(
            self.environment(), catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000
        )
        token = payload["photos"][0]["review_token"]
        status, result = handle_review(
            self.environment(), token, "keep_for_identity", "Best broadside",
            catalog_factory=lambda *_: catalog, epoch_now=1_786_200_001,
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(catalog.reviewed, ("11111111-1111-4111-8111-111111111111", 17, 0, "keep_for_identity", "Best broadside"))
        self.assertEqual(
            handle_review(self.environment(), token + "x", "defer", "", catalog_factory=lambda *_: catalog, epoch_now=1_786_200_001)[0],
            404,
        )
        self.assertEqual(
            handle_review(self.environment(), token, "delete", "", catalog_factory=lambda *_: catalog, epoch_now=1_786_200_001)[0],
            400,
        )

    def test_request_hd_calls_reveal_and_finalizes_durable_request(self):
        class FakeReveal:
            requested = None

            def __init__(self, username, password):
                self.credentials_present = bool(username and password)

            def set_deadline(self, deadline, clock=None):
                self.deadline = deadline

            def request_hd_photos(self, photo_ids):
                type(self).requested = photo_ids
                return {"accepted": len(photo_ids)}

        environment = {
            **self.environment(),
            "TACTACAM_USERNAME": "owner@example.com",
            "TACTACAM_PASSWORD": "secret",
        }
        catalog = MemoryCatalog()
        _, payload = handle_library(
            environment, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000
        )
        token = payload["photos"][0]["review_token"]

        status, result = handle_review(
            environment, token, "request_hd", "Best broadside",
            catalog_factory=lambda *_: catalog, reveal_factory=FakeReveal,
            epoch_now=1_786_200_001,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["request_status"], "submitted")
        self.assertEqual(FakeReveal.requested, ["reveal-photo-1"])
        self.assertEqual(catalog.hd_completed, "33333333-3333-4333-8333-333333333333")
        self.assertIsNone(catalog.reviewed)

    def test_request_hd_failure_is_retryable_and_not_finalized(self):
        class FailingReveal:
            def __init__(self, username, password):
                pass

            def set_deadline(self, deadline, clock=None):
                pass

            def request_hd_photos(self, photo_ids):
                raise HDRequestRejected("private provider detail")

        environment = {
            **self.environment(),
            "TACTACAM_USERNAME": "owner@example.com",
            "TACTACAM_PASSWORD": "secret",
        }
        catalog = MemoryCatalog()
        _, payload = handle_library(
            environment, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000
        )

        status, result = handle_review(
            environment, payload["photos"][0]["review_token"], "request_hd", "",
            catalog_factory=lambda *_: catalog, reveal_factory=FailingReveal,
            epoch_now=1_786_200_001,
        )

        self.assertEqual(status, 502)
        self.assertEqual(result, {"ok": False, "error": "HD request failed"})
        self.assertEqual(
            catalog.hd_failed,
            ("33333333-3333-4333-8333-333333333333", "provider_rejected"),
        )
        self.assertIsNone(catalog.hd_completed)

    def test_ambiguous_hd_provider_result_is_not_automatically_retried(self):
        class TimingOutReveal:
            def __init__(self, username, password):
                pass

            def set_deadline(self, deadline, clock=None):
                pass

            def request_hd_photos(self, photo_ids):
                raise RevealError("response lost after submission")

        environment = {
            **self.environment(),
            "TACTACAM_USERNAME": "owner@example.com",
            "TACTACAM_PASSWORD": "secret",
        }
        catalog = MemoryCatalog()
        _, payload = handle_library(
            environment, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000
        )

        status, result = handle_review(
            environment, payload["photos"][0]["review_token"], "request_hd", "",
            catalog_factory=lambda *_: catalog, reveal_factory=TimingOutReveal,
            epoch_now=1_786_200_001,
        )

        self.assertEqual(status, 202)
        self.assertEqual(result["request_status"], "unknown")
        self.assertEqual(
            catalog.hd_unknown,
            ("33333333-3333-4333-8333-333333333333", "provider_outcome_unknown"),
        )
        self.assertIsNone(catalog.hd_failed)
        self.assertIsNone(catalog.hd_completed)


class Gate1ReviewUiTests(unittest.TestCase):
    def test_review_ui_is_model_selected_and_actionable(self):
        script = Path("public/app.js").read_text()
        self.assertIn("gate1.route === 'review'", script)
        self.assertIn("/api/review", script)
        for action in ("request_hd", "keep_for_identity", "not_useful", "defer"):
            self.assertIn(action, script)

    def test_review_ui_advances_one_card_and_preloads_five_without_full_refresh(self):
        html = Path("public/index.html").read_text()
        script = Path("public/app.js").read_text()
        self.assertIn('id="review-stage"', html)
        self.assertIn("preloadReviewQueue(5)", script)
        self.assertIn("review-enter-right", script)
        self.assertIn("review-exit-left", script)
        submit_body = script.split("async function submitReview", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("fetchLibrary()", submit_body)
        self.assertGreater(submit_body.index("refreshReviewBuffer()"), submit_body.index("await fetch('/api/review'"))
        self.assertIn("pendingReviewIds.size === 0", submit_body)
        self.assertIn("refreshReviewBuffer", script)

    def test_overview_visualizes_the_gate1_narrowing_funnel(self):
        html = Path("public/index.html").read_text()
        script = Path("public/app.js").read_text()
        self.assertIn("Gate 1 narrowing", html)
        for element_id in (
            "pipeline-total", "pipeline-assessed", "pipeline-review",
            "pipeline-duplicates", "pipeline-blanks", "pipeline-nontarget",
            "pipeline-pending",
        ):
            self.assertIn(f'id="{element_id}"', html)
            self.assertIn(element_id, script)
        self.assertIn("Most recent 60 shown", html)


if __name__ == "__main__":
    unittest.main()
