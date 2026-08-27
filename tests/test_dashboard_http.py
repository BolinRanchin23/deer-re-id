import json
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from api.library import handler as LibraryHandler
from api.library_preview import handler as LibraryPreviewHandler
from api.profile_assignment import handler as ProfileAssignmentHandler
from api.review import handler as ReviewHandler
from api.status import handler as StatusHandler
from api.profile_gallery import handler as ProfileGalleryHandler


class DashboardHttpAdapterTests(unittest.TestCase):
    def test_refreshed_information_architecture_and_workflows(self):
        html = Path("public/index.html").read_text()
        js = Path("public/app.js").read_text()
        nav = html.split('<nav class="nav-list">',1)[1].split('</nav>',1)[0]
        labels = [nav.index(label) for label in (">Big Picture<", ">Profiling<", ">Deer<", ">Other<")]
        self.assertEqual(labels, sorted(labels))
        self.assertNotIn('data-view="review"', nav)
        for label in ("All Photos", "Cameras", "Automation Audit"):
            self.assertIn(label, nav)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn("event.key === 'Escape'", js)
        self.assertIn("name === 'review'", js)

    def test_process_all_photos_profiling_and_deer_markup(self):
        html = Path("public/index.html").read_text()
        js = Path("public/app.js").read_text()
        overview = html.split('id="view-overview"',1)[1].split('id="view-review"',1)[0]
        self.assertIn("Process Overview", overview)
        self.assertIn("Last 24 hours", overview)
        self.assertIn("Last 7 days", overview)
        for label in ("Photos received", "Male deer / visible antlers", "Animal crops", "HD photos requested", "Deer profiles"):
            self.assertIn(label, overview)
        for removed in ("Latest from the ranch", "Archive integrity", "Recent ingestion runs", "Gate 1 narrowing"):
            self.assertNotIn(removed, overview)
        for control in ("photo-date-from", "photo-date-to", "photo-time-of-day", "photo-hour-from", "photo-species", "photo-sort", "photos-load-more", "hd-location-filter"):
            self.assertIn(control, html)
        self.assertIn("AbortController", js)
        self.assertIn("/api/photos", js)
        self.assertIn("animals remaining", js)
        self.assertIn("<dialog", html)
        self.assertIn("Set as representative", js)
        self.assertIn("Reassign to", js)
        self.assertIn("/api/profile_gallery?", js)
        self.assertIn("encodeURIComponent(profileId)", js)
        self.assertNotIn("controls.append(select, button)", js)

    def test_big_picture_initialization_does_not_render_removed_photo_or_count_nodes(self):
        js = Path("public/app.js").read_text()
        active = js.split("function renderActiveImageView", 1)[1].split("function showView", 1)[0]
        self.assertNotIn("recent-photos", active)
        update = js.split("function updateCatalogViews", 1)[1].split("async function fetchLibrary", 1)[0]
        self.assertIn("if (target) target.textContent = value", update)

    def test_deer_cards_use_one_square_representative_photo(self):
        js = Path("public/app.js").read_text()
        html = Path("public/index.html").read_text()
        self.assertIn("(profile.previewUrls || []).slice(0, 1)", js)
        self.assertIn(".profile-thumbnail-strip { display: block; aspect-ratio: 1;", html)

    def test_all_photos_exposes_and_sends_extended_filters(self):
        js = Path("public/app.js").read_text()
        html = Path("public/index.html").read_text()
        for field in ("photo-male-antler", "photo-profile-status", "photo-identity-status", "photo-variant"):
            self.assertIn(f'id="{field}"', html)
            self.assertIn(field, js)

    def test_profiling_decision_decrements_selected_camera_progress(self):
        js = Path("public/app.js").read_text()
        self.assertIn("hdReviewProgress.by_camera[item.camera_id]", js)

    def test_status_library_and_preview_are_vercel_python_handlers(self):
        self.assertTrue(issubclass(StatusHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(LibraryHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(LibraryPreviewHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(ProfileAssignmentHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(ReviewHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(ProfileGalleryHandler, BaseHTTPRequestHandler))

    def test_private_immutable_previews_are_browser_cacheable(self):
        adapter = Path("api/library_preview.py").read_text(encoding="utf-8")
        self.assertIn('"private, max-age=300, immutable" if status == 200 else "private, no-store"', adapter)
        self.assertNotIn('self.send_header("Cache-Control", "private, no-store")', adapter)

    def test_root_homepage_contains_operational_dashboard_shell(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        self.assertIn("DeerID Workspace", html)
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn("/api/status", app_js)
        self.assertNotIn("Recent ingestion runs", html)
        self.assertNotIn("Archive integrity", html)
        self.assertNotIn("SUPABASE_SECRET_KEY", html)
        self.assertNotIn("CRON_SECRET", html)
        self.assertNotIn("Fort McKavett", html)
        self.assertNotIn("Recent photos", html)
        self.assertNotIn("renderPreviews", app_js)
        self.assertNotIn("Archived photos are never served by this dashboard", html)
        self.assertNotIn('<span class="check">✓</span>', html)
        compact_js = "".join(app_js.split())
        self.assertNotIn("Math.min(n(verified.image),n(verified.metadata),n(verified.checksum))", compact_js)
        self.assertNotIn("n(run.downloaded), n(run.skipped), n(run.failed)", app_js)
        self.assertIn("Photo archive", html)
        self.assertIn("camera map", html)
        self.assertIn("Satellite", html)
        self.assertIn("/api/library", app_js)
        self.assertIn("server.arcgisonline.com", app_js)
        self.assertNotIn("/api/auth", app_js)
        self.assertNotIn("Sign in", html)
        self.assertNotIn("Forgot password", html)
        for view in ("Big Picture", "Profiling", "Deer", "Cameras", "All Photos"):
            self.assertIn(view, html)
        self.assertIn('id="workspace-shell"', html)
        self.assertNotIn("sessionStorage", html + app_js)
        self.assertNotIn("<script>", html)
        self.assertIn('<script src="/app.js?v=22" defer></script>', html)
        self.assertIn('id="24h-photos"', html)
        self.assertIn('id="7d-photos"', html)
        self.assertIn("processOverview.last_24_hours", app_js)
        self.assertNotIn("GOOGLE_MAPS_BROWSER_KEY", html)

    def test_catalog_only_materializes_images_for_the_active_view(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        update = app_js.split("function updateCatalogViews", 1)[1].split("async function fetchLibrary", 1)[0]
        self.assertIn("renderActiveImageView()", update)
        for eager_hidden_renderer in (
            "renderFilteredPhotos();",
            "renderDeerProfiles();",
            "renderAutomationAudit();",
            "renderHDReview();",
        ):
            self.assertNotIn(eager_hidden_renderer, update)
        show = app_js.split("function showView", 1)[1].split("function updateCatalogViews", 1)[0]
        self.assertIn("renderActiveImageView()", show)

    def test_profile_gallery_invalidates_stale_same_profile_requests(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        gallery = app_js.split("async function openProfileGallery", 1)[1].split("function renderDeerProfiles", 1)[0]
        self.assertIn("const requestGeneration=++profileGalleryRequestGeneration", gallery)
        self.assertIn("requestGeneration!==profileGalleryRequestGeneration", gallery)
        back = app_js.split("'profile-gallery-back'", 1)[1].split("document.querySelectorAll('[data-review-queue]')", 1)[0]
        self.assertIn("profileGalleryRequestGeneration++", back)

    def test_mobile_review_places_consolidated_quick_actions_beside_the_photo(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        compact_js = "".join(app_js.split())
        self.assertIn("card.append(image,quickActions,meta)", compact_js)
        self.assertIn("Pass → Request HD", app_js)
        self.assertNotIn("Keep for ID", app_js)
        self.assertIn("Create new deer profile", app_js)
        self.assertIn("Add photo to profile", app_js)
        self.assertIn("/api/profile_assignment", app_js)
        compact_html = "".join(html.split())
        self.assertIn("grid-template-columns:repeat(3,1fr)", compact_html)
        self.assertIn(".profile-create-rowinput,.profile-create-rowbutton{grid-column:1/-1", compact_html)

    def test_returned_hd_actions_are_directly_below_species_and_sex(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        review = app_js.split("function renderHDReview", 1)[1].split("async function submitHDReviewDecision", 1)[0]
        compact = "".join(review.split())
        self.assertIn("meta.append(instance,heading,decisionPrompt,controls,modelDetails)", compact)
        self.assertIn("skip.textContent='Notidentifiable'", compact)
        self.assertNotIn("setAttribute('aria-label','Moreactions')", compact)

    def test_returned_hd_actions_use_explicit_decision_labels(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        review = app_js.split("function renderHDReview", 1)[1].split("async function submitHDReviewDecision", 1)[0]
        self.assertIn("Match existing deer", review)
        self.assertIn("Create new deer", review)
        self.assertIn("Not identifiable", review)
        self.assertNotIn("create.textContent='+'", "".join(review.split()))
        self.assertNotIn("match.textContent='✎'", "".join(review.split()))

    def test_returned_hd_defaults_to_a_simple_decision_with_expandable_model_analysis(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        review = app_js.split("function renderHDReview", 1)[1].split("async function submitHDReviewDecision", 1)[0]
        compact = "".join(review.split())
        self.assertIn("What should happen with this deer?", review)
        self.assertIn("View full model analysis", review)
        self.assertIn("modelDetails.append(modelSummary,modelAnalysis)", compact)
        self.assertNotIn("modelDetails.open", review)
        self.assertIn("meta.append(instance,heading,decisionPrompt,controls,modelDetails)", compact)

    def test_expanded_model_analysis_uses_scannable_labeled_sections(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        html = Path("public/index.html").read_text(encoding="utf-8")
        review = app_js.split("function renderHDReview", 1)[1].split("async function submitHDReviewDecision", 1)[0]
        for label in ("Identity description", "View", "Visible tines", "Antlers", "Visibility limits", "Age class", "Age cues", "Detection"):
            self.assertIn(label, review)
        self.assertIn("hd-model-facts", review)
        self.assertIn("document.createElement('dt')", review)
        self.assertIn("document.createElement('dd')", review)
        self.assertIn(".hd-model-facts", html)

    def test_existing_profile_picker_suggests_profiles_seen_at_the_photo_location(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        picker = app_js.split("function openProfilePicker", 1)[1].split("function openCreateProfile", 1)[0]
        self.assertIn("Suggested at", picker)
        self.assertIn("profile.cameraIds.includes(item.camera_id)", picker)
        self.assertIn("profile.photoCount", picker)
        self.assertIn("Other profiles", picker)
        html = Path("public/index.html").read_text(encoding="utf-8")
        self.assertIn(".profile-picker-section", html)
        self.assertIn(".profile-picker-option", html)

    def test_returned_hd_review_is_scoped_to_one_animal_instance(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn("Reviewing deer ${item.instance_index} of ${item.instance_count} from this photo", app_js)
        self.assertIn("hd-instance-crop", app_js)
        self.assertIn("hd-context-box", app_js)
        self.assertIn("item.hd_animal_instance_id", app_js)

        self.assertIn(".hd-context-box", html)

    def test_returned_hd_review_states_which_deer_from_the_photo_is_being_reviewed(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn("Reviewing deer ${item.instance_index} of ${item.instance_count} from this photo", app_js)

    def test_returned_hd_visuals_label_the_selected_crop_and_original_photo(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        review = app_js.split("function renderHDReview", 1)[1].split("async function submitHDReviewDecision", 1)[0]
        self.assertIn("Selected deer crop", review)
        self.assertIn("Original photo", review)

    def test_original_photo_highlight_container_does_not_stretch_away_from_the_image(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        compact = "".join(html.split())
        self.assertIn(".hd-instance-context{position:relative", compact)
        self.assertIn("align-self:start", compact.split(".hd-instance-context{position:relative", 1)[1].split("}", 1)[0])

    def test_review_badge_counts_only_actionable_cards_and_explains_model_backlog(self):
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        compact = "".join(app_js.split())
        self.assertIn("constactionable=photos.filter(belongsToActiveQueue).length", compact)
        self.assertIn("review-nav-count').textContent=actionable", compact)
        self.assertIn("awaitingGemmarouting", compact)

    def test_returned_hd_page_has_authoritative_progress_meter(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn('id="hd-review-progress"', html)
        self.assertIn('id="hd-review-progress-bar"', html)
        self.assertIn('id="hd-review-progress-copy"', html)
        self.assertIn("data.hd_review_progress", app_js)
        self.assertIn("animals remaining", app_js)

    def test_vercel_config_sets_static_dashboard_security_headers_and_only_gate1b_cron(self):
        config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(config["crons"],[{"path":"/api/gate1b_cron","schedule":"*/15 * * * *"}])
        self.assertIn("api/library.py", config["functions"])
        self.assertIn("api/library_preview.py", config["functions"])
        self.assertIn("api/profile_assignment.py", config["functions"])
        self.assertIn("api/review.py", config["functions"])
        self.assertNotIn("api/auth.py", config["functions"])
        root = next(item for item in config["headers"] if item["source"] == "/")
        headers = {item["key"]: item["value"] for item in root["headers"]}
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("server.arcgisonline.com", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(
            "'unsafe-inline'", headers["Content-Security-Policy"].split("style-src")[0]
        )
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main()
