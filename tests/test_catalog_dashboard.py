import unittest
from pathlib import Path

from reveal_downloader.catalog import (
    LIBRARY_DEADLINE_SECONDS,
    _sanitize_photos,
    _sanitize_profiles,
    _sanitize_process_overview,
    _sign_aux_action_token,
    handle_gate1b_label,
    handle_automation_label,
    handle_library,
    handle_library_preview,
    handle_profile_assignment,
    handle_profile_gallery,
    handle_profile_reassignment,
    handle_profile_representative,
    handle_photos_query,
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
        self.profile_created = None
        self.profile_attached = None
        self.operational_stats = None
        self.automation_labeled = None
        self.library_limits = []
        self.profile_reassigned = None
        self.profile_representative = None
        self.photos_query = None

    def set_deadline(self, deadline, clock=None):
        self.deadline = deadline
        self.clock = clock

    def read_library(self, limit=60):
        self.library_limits.append(limit)
        return [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "captured_at": "2026-08-08T12:00:00Z",
                "camera_id": "22222222-2222-4222-8222-222222222222",
                "camera_name": "North Ridge",
                "variant": "cloud_thumbnail",
                "width": 1280,
                "height": 720,
                "labels": [
                    {"namespace": "species", "label": "deer", "status": "suggested"}
                ],
                "animals": [],
                "gate1": {
                    "id": 17,
                    "review_version": 0,
                    "route": "review",
                    "reason": "target_species",
                    "animal_confidence": 0.98,
                    "species_label": "white-tailed deer",
                    "species_confidence": 0.99,
                    "model_name": "SpeciesNet",
                    "model_version": "4.0.3a",
                },
                "review_decision": None,
                "object_path": "must-not-leak.jpg",
            }
        ][:limit]

    def read_profiles(self):
        return [
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "animal_id": "55555555-5555-4555-8555-555555555555",
                "display_name": "Wide Ten",
                "species": "white-tailed deer",
                "sex": "male",
                "season_year": 2026,
                "photo_count": 3,
                "profile_previews": [{
                    "media_id": "11111111-1111-4111-8111-111111111111",
                    "media_asset_id": "66666666-6666-4666-8666-666666666666",
                    "hd_animal_instance_id": "77777777-7777-4777-8777-777777777777",
                    "assignment_event_id": 71,
                    "bbox": {"x": .1, "y": .2, "width": .5, "height": .6},
                    "crop_recipe": {"kind": "normalized_bbox"},
                    "is_representative": True,
                }],
                "representative_assignment_event_id": 71,
            }
        ]

    def read_profile_gallery_page(self, profile_id, limit=60):
        self.profile_gallery_query = (profile_id, limit)
        return [{
            "assignment_event_id": 71,
            "hd_animal_instance_id": "77777777-7777-4777-8777-777777777777",
            "animal_profile_id": "44444444-4444-4444-8444-444444444444",
            "media_id": "11111111-1111-4111-8111-111111111111",
            "media_asset_id": "66666666-6666-4666-8666-666666666666",
            "captured_at": "2026-08-08T12:00:00Z",
            "camera_name": "North Ridge",
            "bbox": {"x": .1, "y": .2, "width": .5, "height": .6},
            "crop_recipe": {"kind": "normalized_bbox"},
        }][:limit] if profile_id == "44444444-4444-4444-8444-444444444444" else []

    def reassign_hd_instance(self, assignment_event_id, profile_id):
        self.profile_reassigned = (assignment_event_id, profile_id)
        return {"ok": True, "assignment_event_id": 72, "profile_id": profile_id}

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

    def read_gate1b_metrics(self):
        return {
            "model_name": "OpenAI-GPT-4o-mini-Vision",
            "model_version": "gpt-4o-mini-2024-07-18@prompt-2026-08-12.1",
            "predictions": 0,
            "likely_male": 0,
            "uncertain": 0,
            "female_candidates": 0,
            "human_labels": 0,
            "labeled_buck_events": 0,
            "labeled_cameras": 0,
            "labeled_day": 0,
            "labeled_ir": 0,
            "labeled_axis": 0,
            "buck_recall": None,
            "suppression_enabled": False,
            "suppression_ready": False,
            "female_audit_percent": 10,
            "minimum_labels": 100,
            "minimum_buck_events": 20,
            "required_buck_recall": 0.99,
        }

    def read_operational_stats(self):
        self.operational_stats = True
        return {
            "photos_received_24h": 12,
            "hd_requests_24h": 3,
            "hd_available_24h": 2,
            "as_of": "2026-08-12T01:00:00Z",
        }

    def read_automation_audit(self, limit=120):
        return [{"automation_event_id": 8, "media_id": "11111111-1111-4111-8111-111111111111", "action": "auto_suppress_female", "captured_at": "2026-08-08T12:00:00Z", "camera_name": "North Ridge", "prediction": {"species_label": "whitetail", "visible_antler": "no", "probable_male": "no", "head_visibility": "full", "triage_class": "female_candidate", "reason": "clear antlerless deer"}, "human_verdict": None, "human_note": None}]

    def read_process_overview(self):
        return {"as_of":"2026-08-12T01:00:00Z","last_24_hours":{"from":"2026-08-11T01:00:00Z","to":"2026-08-12T01:00:00Z","photos_received":12,"male_or_antler":8,"animal_crops":5,"hd_requests":3,"profiles":2},"last_7_days":{"from":"2026-08-05T01:00:00Z","to":"2026-08-12T01:00:00Z","photos_received":120,"male_or_antler":80,"animal_crops":50,"hd_requests":30,"profiles":12}}

    def query_all_photos(self, filters):
        self.photos_query = filters
        return {"items": self.read_library(filters["limit"]), "next_cursor": "cursor-2", "total": 81, "facets": {"species":["deer"]}}

    def set_profile_representative(self, assignment_event_id, profile_id):
        self.profile_representative = (assignment_event_id, profile_id)
        return {"ok": True, "profile_id": profile_id, "assignment_event_id": assignment_event_id}

    def read_hd_review_queue(self, limit=60):
        return [{"hd_review_result_id": 4, "media_id": "11111111-1111-4111-8111-111111111111", "media_asset_id": "66666666-6666-4666-8666-666666666666", "model_name": "Ollama-Gemma4-Vision-HD", "model_version": "hd-v1", "captured_at": "2026-08-08T12:00:00Z", "camera_name": "North Ridge", "result": {"species": "whitetail", "sex": "male", "animal_count": 1, "identity_eligible": True, "age_eligible": False, "age_class": "unknown", "antler_score_eligible": False, "antler_score_range": "unknown", "distinguishing_features": ["split brow"], "summary": "Useful identity image"}}]

    def read_hd_review_progress(self):
        return {"total": 12, "completed": 5, "remaining": 7}

    def record_automation_label(self, event_id, verdict, note=""):
        self.automation_labeled = (event_id, verdict, note)
        return {"ok": True, "label_id": 11}

    def record_gate1b_label(
        self,
        media_id,
        assessment_id,
        review_version,
        species_label,
        visible_antler,
        probable_male,
        head_visibility,
        note,
    ):
        self.gate1b_label = (
            media_id,
            assessment_id,
            review_version,
            species_label,
            visible_antler,
            probable_male,
            head_visibility,
            note,
        )
        return {"ok": True, "label_id": 9}

    def resolve_media_object(self, media_id):
        self.resolved = media_id
        return {
            "object_path": "cam@"
            + "a" * 64
            + "/2026/08/08/20260808T120000Z_photo@"
            + "b" * 64
            + ".jpg",
            "content_type": "image/jpeg",
        }

    def resolve_media_asset_object(self, media_asset_id):
        self.resolved_asset = media_asset_id
        return {"object_path": "cam@" + "a" * 64 + "/2026/08/08/20260808T120000Z_123456789012345-123-4-12345678901234-ab-cdef12345.jpg@" + "c" * 64 + "_hd.jpg", "content_type": "image/jpeg"}

    def read_private_image(self, object_path, max_bytes):
        self.read_args = (object_path, max_bytes)
        return b"\xff\xd8private\xff\xd9"

    def record_review(self, media_id, assessment_id, review_version, action, note):
        self.reviewed = (media_id, assessment_id, review_version, action, note)
        return {"ok": True, "media_id": media_id, "action": action}

    def create_profile_from_review(
        self, media_id, assessment_id, review_version, display_name, species, sex, notes
    ):
        self.profile_created = (
            media_id,
            assessment_id,
            review_version,
            display_name,
            species,
            sex,
            notes,
        )
        return {
            "ok": True,
            "profile_id": "44444444-4444-4444-8444-444444444444",
        }

    def attach_media_to_profile(
        self, media_id, assessment_id, review_version, profile_id
    ):
        self.profile_attached = (
            media_id,
            assessment_id,
            review_version,
            profile_id,
        )
        return {"ok": True, "profile_id": profile_id}

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
    def test_library_deadline_accommodates_full_operational_dashboard(self):
        self.assertGreaterEqual(LIBRARY_DEADLINE_SECONDS, 20)

    def test_process_overview_requires_both_windows_and_non_negative_counts(self):
        catalog = MemoryCatalog()
        overview = _sanitize_process_overview(catalog.read_process_overview())
        self.assertEqual(overview["last_24_hours"]["profiles"], 2)
        broken = catalog.read_process_overview()
        broken["last_7_days"]["animal_crops"] = -1
        with self.assertRaises(Exception):
            _sanitize_process_overview(broken)

    def test_library_bounds_hd_review_bootstrap_to_transport_safe_slice(self):
        source=Path("reveal_downloader/catalog.py").read_text()
        self.assertIn("catalog.read_hd_review_queue(5)",source)
        self.assertNotIn("catalog.read_hd_review_queue(10)",source)

    def test_all_photos_default_page_allows_thirty_signed_rows(self):
        catalog = MemoryCatalog()
        row = catalog.read_library(1)[0]
        catalog.query_all_photos = lambda filters: {"items": [dict(row, id=f"00000000-0000-4000-8000-{index:012d}") for index in range(30)], "next_cursor": None, "total": 30, "facets": {}}
        status, payload = handle_photos_query(self.environment(), {"limit":"30","sort":"newest","time_of_day":"all"}, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 30)

    def test_all_photos_rejects_rpc_rows_beyond_requested_limit(self):
        catalog = MemoryCatalog()
        row = catalog.read_library(1)[0]
        catalog.query_all_photos = lambda filters: {
            "items": [row for _ in range(31)], "total": 31, "next_cursor": None, "facets": {}
        }
        status, payload = handle_photos_query(
            self.environment(), {"limit": "30"}, catalog_factory=lambda *_: catalog, epoch_now=1_700_000_000
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid filter")

    def test_profile_representative_crop_rejects_unsafe_bbox(self):
        catalog = MemoryCatalog()
        profiles = catalog.read_profiles()
        profiles[0]["profile_previews"][0]["bbox"] = {
            "x": 0.1, "y": 0.1, "width": 5000000, "height": 0.5
        }
        with self.assertRaises(Exception):
            _sanitize_profiles(profiles, b"s" * 32, 1_700_000_000)

    def test_all_photos_query_validates_filters_and_returns_signed_page(self):
        catalog = MemoryCatalog()
        status, payload = handle_photos_query(self.environment(), {"limit":"25","sort":"oldest","time_of_day":"night","camera_id":"22222222-2222-4222-8222-222222222222"}, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 81)
        self.assertEqual(catalog.photos_query["limit"], 25)
        self.assertRegex(payload["items"][0]["preview_url"], r"^/api/library_preview")
        self.assertEqual(handle_photos_query(self.environment(), {"sort":"random"}, catalog_factory=lambda *_: catalog)[0], 400)

    def test_representative_selection_uses_signed_assignment_capability(self):
        catalog = MemoryCatalog()
        token = _sign_aux_action_token(71, "representative", 1_786_200_900, b"preview-signing-secret-at-least-16")
        profile_id = "44444444-4444-4444-8444-444444444444"
        status, payload = handle_profile_representative(self.environment(), token, profile_id, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_001)
        self.assertEqual(status, 200)
        self.assertEqual(catalog.profile_representative, (71, profile_id))
        self.assertEqual(handle_profile_representative(self.environment(), token + "x", profile_id, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_001)[0], 404)

    def test_hd_crop_uses_aspect_ratio_without_fill_distortion(self):
        app = (Path(__file__).parents[1] / "public" / "app.js").read_text()
        page = (Path(__file__).parents[1] / "public" / "index.html").read_text()
        self.assertIn("crop.style.aspectRatio", app)
        self.assertNotIn("cropImage.style.height", app)
        self.assertNotIn("object-fit: fill", page)
        self.assertIn("object-fit: contain", page)
        self.assertNotIn(".hd-instance-crop { min-height:", page)

    def test_postgrest_auth_distinguishes_modern_keys_from_legacy_jwts(self):
        self.assertEqual(
            _postgrest_auth_headers("sb_secret_test"), {"apikey": "sb_secret_test"}
        )
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
        self.assertEqual(catalog.library_limits, [10])
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["photos"]), 1)
        self.assertEqual(len(payload["cameras"]), 1)
        self.assertEqual(payload["profiles"][0]["display_name"], "Wide Ten")
        self.assertEqual(payload["profiles"][0]["photo_count"], 3)
        self.assertRegex(payload["profiles"][0]["representative_crop"]["preview_url"], r"^/api/library_preview\?token=asset\.")
        self.assertEqual(payload["profiles"][0]["representative_crop"]["bbox"]["x"], .1)
        self.assertNotIn("profile_gallery", payload)
        self.assertEqual(payload["mapbox_access_token"], "pk.mapbox-browser-token")
        self.assertRegex(payload["photos"][0]["preview_url"], r"^/api/library_preview\?token=")
        self.assertRegex(payload["photos"][0]["review_token"], r"^[0-9]+\.")
        self.assertEqual(payload["photos"][0]["gate1"]["route"], "review")
        self.assertEqual(payload["pipeline"]["total_thumbnails"], 100)
        self.assertEqual(payload["pipeline"]["review_representatives"], 60)
        self.assertEqual(payload["pipeline"]["model_name"], "SpeciesNet")
        self.assertEqual(payload["stats"]["photos_received_24h"], 12)
        self.assertEqual(payload["stats"]["hd_requests_24h"], 3)
        self.assertEqual(payload["stats"]["hd_available_24h"], 2)
        self.assertEqual(payload["automation_audit"][0]["action"], "auto_suppress_female")
        self.assertEqual(payload["hd_review_queue"][0]["result"]["identity_eligible"], True)
        self.assertRegex(payload["automation_audit"][0]["preview_url"], r"^/api/library_preview\?token=")
        self.assertRegex(payload["hd_review_queue"][0]["preview_url"], r"^/api/library_preview\?token=asset\.")
        asset_token = payload["hd_review_queue"][0]["preview_url"].split("token=", 1)[1]
        preview_status, _, preview_body = handle_library_preview(
            self.environment(), asset_token, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000
        )
        self.assertEqual(preview_status, 200)
        self.assertEqual(preview_body, b"\xff\xd8private\xff\xd9")
        self.assertEqual(catalog.resolved_asset, "66666666-6666-4666-8666-666666666666")
        self.assertTrue(catalog.operational_stats)
        serialized = str(payload)
        self.assertNotIn("must-not-leak.jpg", serialized)
        self.assertNotIn("SUPABASE_SECRET_KEY", serialized)

    def test_profile_gallery_is_loaded_per_profile_in_a_bounded_request(self):
        catalog = MemoryCatalog()
        profile_id = "44444444-4444-4444-8444-444444444444"
        status, payload = handle_profile_gallery(
            self.environment(),
            {"profile_id": profile_id, "limit": "24"},
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_000,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(catalog.profile_gallery_query, (profile_id, 24))
        self.assertEqual(payload["items"][0]["profile_id"], profile_id)
        self.assertRegex(payload["items"][0]["preview_url"], r"^/api/library_preview\?token=asset\.")
        self.assertEqual(
            handle_profile_gallery(
                self.environment(), {"profile_id": "not-a-uuid"},
                catalog_factory=lambda *_: catalog,
            )[0],
            400,
        )

    def test_profile_gallery_rejects_rpc_rows_beyond_the_requested_limit(self):
        class OversizedGalleryCatalog(MemoryCatalog):
            def read_profile_gallery_page(self, profile_id, limit=60):
                row = super().read_profile_gallery_page(profile_id, limit)[0]
                return [dict(row) for _ in range(limit + 1)]

        status, payload = handle_profile_gallery(
            self.environment(),
            {"profile_id": "44444444-4444-4444-8444-444444444444", "limit": "24"},
            catalog_factory=lambda *_: OversizedGalleryCatalog(),
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"ok": False, "error": "profile gallery unavailable"})

    def test_profile_gallery_rejects_malformed_unbounded_metadata(self):
        class MalformedGalleryCatalog(MemoryCatalog):
            def read_profile_gallery_page(self, profile_id, limit=60):
                row = super().read_profile_gallery_page(profile_id, limit)[0]
                row["camera_name"] = {"unexpected": "mapping"}
                return [row]

        status, payload = handle_profile_gallery(
            self.environment(),
            {"profile_id": "44444444-4444-4444-8444-444444444444", "limit": "24"},
            catalog_factory=lambda *_: MalformedGalleryCatalog(),
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"ok": False, "error": "profile gallery unavailable"})

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
                self.environment(),
                token + "x",
                catalog_factory=lambda *_: catalog,
                epoch_now=1_786_200_001,
            )[0],
            404,
        )

    def test_pending_hd_item_is_not_issued_an_actionable_review_token(self):
        photos = _sanitize_photos(
            [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "gate1": {
                        "id": 17,
                        "route": "review",
                        "review_version": 0,
                        "pending_hd": True,
                    },
                    "review_decision": None,
                }
            ],
            b"0123456789abcdef",
            1_786_200_000,
        )
        self.assertNotIn("review_token", photos[0])
        self.assertTrue(photos[0]["gate1"]["pending_hd"])

    def test_signed_review_token_records_allowed_action_and_rejects_tampering(self):
        catalog = MemoryCatalog()
        _, payload = handle_library(
            self.environment(),
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_000,
        )
        token = payload["photos"][0]["review_token"]
        status, result = handle_review(
            self.environment(),
            token,
            "keep_for_identity",
            "Best broadside",
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_001,
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(
            catalog.reviewed,
            (
                "11111111-1111-4111-8111-111111111111",
                17,
                0,
                "keep_for_identity",
                "Best broadside",
            ),
        )
        self.assertEqual(
            handle_review(
                self.environment(),
                token + "x",
                "defer",
                "",
                catalog_factory=lambda *_: catalog,
                epoch_now=1_786_200_001,
            )[0],
            404,
        )
        self.assertEqual(
            handle_review(
                self.environment(),
                token,
                "delete",
                "",
                catalog_factory=lambda *_: catalog,
                epoch_now=1_786_200_001,
            )[0],
            400,
        )

    def test_profile_assignment_can_create_and_attach_without_resolving_review(self):
        environment = self.environment()
        catalog = MemoryCatalog()
        _, payload = handle_library(
            environment, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000
        )
        token = payload["photos"][0]["review_token"]

        status, result = handle_profile_assignment(
            environment,
            token,
            "create",
            display_name="Wide Ten",
            species="white-tailed deer",
            sex="male",
            notes="Split brow tine",
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_001,
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(
            catalog.profile_created[3:],
            ("Wide Ten", "white-tailed deer", "male", "Split brow tine"),
        )
        self.assertIsNone(catalog.reviewed)

        profile_id = "44444444-4444-4444-8444-444444444444"
        status, result = handle_profile_assignment(
            environment,
            token,
            "attach",
            profile_id=profile_id,
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_001,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["profile_id"], profile_id)
        self.assertEqual(catalog.profile_attached[-1], profile_id)

    def test_profile_assignment_rejects_invalid_or_tampered_input(self):
        catalog = MemoryCatalog()
        _, payload = handle_library(
            self.environment(),
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_000,
        )
        token = payload["photos"][0]["review_token"]
        self.assertEqual(
            handle_profile_assignment(
                self.environment(),
                token,
                "create",
                display_name="",
                species="white-tailed deer",
                sex="male",
                catalog_factory=lambda *_: catalog,
                epoch_now=1_786_200_001,
            )[0],
            400,
        )
        self.assertEqual(
            handle_profile_assignment(
                self.environment(),
                token + "x",
                "attach",
                profile_id="44444444-4444-4444-8444-444444444444",
                catalog_factory=lambda *_: catalog,
                epoch_now=1_786_200_001,
            )[0],
            404,
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
            environment,
            token,
            "request_hd",
            "Best broadside",
            catalog_factory=lambda *_: catalog,
            reveal_factory=FakeReveal,
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
            environment,
            payload["photos"][0]["review_token"],
            "request_hd",
            "",
            catalog_factory=lambda *_: catalog,
            reveal_factory=FailingReveal,
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
            environment,
            payload["photos"][0]["review_token"],
            "request_hd",
            "",
            catalog_factory=lambda *_: catalog,
            reveal_factory=TimingOutReveal,
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

    def test_gate1b_human_correction_is_appended_without_resolving_review(self):
        environment = self.environment()
        catalog = MemoryCatalog()
        _, payload = handle_library(
            environment, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000
        )
        status, result = handle_gate1b_label(
            environment,
            payload["photos"][0]["review_token"],
            "axis",
            "yes",
            "yes",
            "full",
            "spotted axis buck",
            catalog_factory=lambda *_: catalog,
            epoch_now=1_786_200_001,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["label_id"], 9)
        self.assertEqual(
            catalog.gate1b_label,
            (
                "11111111-1111-4111-8111-111111111111",
                17,
                0,
                "axis",
                "yes",
                "yes",
                "full",
                "spotted axis buck",
            ),
        )
        self.assertIsNone(catalog.reviewed)

    def test_gate1b_human_correction_rejects_invalid_enumerations(self):
        status, result = handle_gate1b_label(
            self.environment(), "ignored", "doe", "no", "no", "full"
        )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid species label")


class Gate1ReviewUiTests(unittest.TestCase):
    def test_hd_review_is_one_card_at_a_time_and_advances_without_full_refresh(self):
        script = Path("public/app.js").read_text()
        body = script.split("function renderHDReview", 1)[1].split("async function submitHDReviewDecision", 1)[0]
        self.assertIn("const item = locationQueue[0]", body)
        self.assertIn("preloadHDReviewQueue(5)", script)
        submit = script.split("async function submitHDReviewDecision", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("await fetchLibrary()", submit)
        self.assertIn("renderHDReview(true)", submit)

    def test_profile_cards_open_two_column_crop_first_gallery_with_reassignment(self):
        html = Path("public/index.html").read_text()
        script = Path("public/app.js").read_text()
        self.assertIn('id="profile-gallery"', html)
        self.assertIn("grid-template-columns: repeat(2", html)
        self.assertIn("makeInstanceCrop", script)
        self.assertIn("/api/profile_reassignment", script)
        self.assertIn("Reassign to", script)

    def test_profile_reassignment_uses_signed_assignment_event_capability(self):
        catalog = MemoryCatalog()
        token = _sign_aux_action_token(71, "reassign", 1_786_200_100, b"preview-signing-secret-at-least-16")
        target = "056f440d-598a-44dd-8695-cabd04f25be4"
        status, payload = handle_profile_reassignment(
            PrivateLibraryTests.environment(), token, target,
            catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000,
        )
        self.assertEqual(status, 200)
        self.assertEqual(catalog.profile_reassigned, (71, target))
        self.assertEqual(handle_profile_reassignment(PrivateLibraryTests.environment(), token + "x", target, catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000)[0], 404)

    def test_review_ui_is_model_selected_and_actionable(self):
        script = Path("public/app.js").read_text()
        self.assertIn("gate1.route === 'review'", script)
        self.assertIn("/api/review", script)
        for action in ("request_hd", "not_useful", "defer"):
            self.assertIn(action, script)
        self.assertNotIn("keep_for_identity", script)

    def test_review_ui_advances_one_card_and_preloads_five_without_full_refresh(self):
        html = Path("public/index.html").read_text()
        script = Path("public/app.js").read_text()
        self.assertIn('id="review-stage"', html)
        self.assertIn("preloadReviewQueue(5)", script)
        self.assertIn("review-enter-right", script)
        self.assertIn("review-exit-left", script)
        submit_body = script.split("async function submitReview", 1)[1].split(
            "\n}\n", 1
        )[0]
        self.assertNotIn("fetchLibrary()", submit_body)
        self.assertGreater(
            submit_body.index("refreshReviewBuffer()"),
            submit_body.index("await fetch('/api/review'"),
        )
        self.assertIn("pendingReviewIds.size === 0", submit_body)
        self.assertIn("refreshReviewBuffer", script)

    def test_overview_visualizes_two_five_stage_process_windows(self):
        html = Path("public/index.html").read_text()
        script = Path("public/app.js").read_text()
        self.assertIn("Process Overview", html)
        for element_id in ("24h-photos","24h-male","24h-crops","24h-hd","24h-profiles","7d-photos","7d-male","7d-crops","7d-hd","7d-profiles"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("`${prefix}-${id}`", script)
        self.assertIn("`${prefix}-hd`", script)
        self.assertNotIn("Most recent 50 shown", html)

    def test_gate1b_ui_keeps_only_uncertain_in_primary_review_and_adds_audit_workspaces(self):
        html = Path("public/index.html").read_text()
        script = Path("public/app.js").read_text()
        self.assertIn('data-review-queue="uncertain"', html)
        self.assertNotIn('data-review-queue="likely_male"', html)
        self.assertNotIn('data-review-queue="female_audit"', html)
        self.assertIn('data-view-panel="audit"', html)
        self.assertIn('data-view-panel="hdreview"', html)
        self.assertIn("/api/automation_label", script)
        self.assertIn("/api/hd_review_decision", script)
        for field in (
            "species_label",
            "visible_antler",
            "probable_male",
            "head_visibility",
        ):
            self.assertIn(field, script)
        self.assertIn("/api/gate1b_label", script)
        self.assertIn("Save corrections", script)
        self.assertIn("suppression_ready", script)
        self.assertIn("suppression_enabled", script)

    def test_female_candidates_are_not_bulk_suppressed_by_client_code(self):
        script = Path("public/app.js").read_text()
        self.assertNotIn("triage_class === 'female_candidate'", script)
        self.assertIn("gate1b.queue", script)
    def test_profile_cards_receive_up_to_five_recent_or_highlighted_previews(self):
        migration = Path("supabase/migrations/20260812002000_profile_previews_and_hd_field_summary.sql")
        self.assertTrue(migration.exists())
        sql = migration.read_text()
        self.assertIn("deerid_profiles", sql)
        self.assertIn("profile_previews", sql)
        self.assertIn("limit 5", sql.lower())
        script = Path("public/app.js").read_text()
        html = Path("public/index.html").read_text()
        self.assertIn("profile.representativeCrop", script)
        self.assertIn("makeInstanceCrop(profile.representativeCrop", script)
        self.assertIn("profile.preview_urls", script)
        self.assertIn(".profile-thumbnail-strip.representative-photo", html)
        self.assertIn(".profile-representative-crop { width: 100%; height: 100%; aspect-ratio: auto !important", html)
        self.assertIn(".profile-representative-crop canvas { position: absolute; inset: 0; width: 100%; height: 100%", html)
        self.assertIn("object-fit: contain; object-position: center", html)
        self.assertIn("profile-thumbnail-strip", script)

    def test_returned_hd_uses_full_frame_contain_rendering(self):
        html = Path("public/index.html").read_text()
        script = Path("public/app.js").read_text()
        self.assertIn("hd-review-image", script)
        self.assertIn(".hd-review-image", html)
        self.assertIn("object-fit: contain", html)
        self.assertIn("height: auto", html)
        self.assertIn("aria-label','Create profile'", script)

    def test_automation_audit_label_is_append_only_and_action_specific(self):
        catalog = MemoryCatalog()
        token = _sign_aux_action_token(8, "audit", 1_786_200_100, b"preview-signing-secret-at-least-16")
        status, payload = handle_automation_label(
            PrivateLibraryTests.environment(), token, "should_have_requested_hd", "missed buck",
            catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(catalog.automation_labeled, (8, "should_have_requested_hd", "missed buck"))
        self.assertEqual(handle_automation_label(PrivateLibraryTests.environment(), token, "bad", catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000)[0], 404)
        self.assertEqual(handle_automation_label(PrivateLibraryTests.environment(), "forged", "correct", catalog_factory=lambda *_: catalog, epoch_now=1_786_200_000)[0], 404)


if __name__ == "__main__":
    unittest.main()
