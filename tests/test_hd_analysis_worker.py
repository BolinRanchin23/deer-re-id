import unittest

from reveal_downloader import hd_analysis_worker


class FakeCatalog:
    instance = None

    def __init__(self, *_args):
        type(self).instance = self
        self.completed = None

    def set_deadline(self, deadline, *, clock):
        self.deadline = deadline

    def claim_hd_review(self, model_name, model_version):
        self.claim_args = (model_name, model_version)
        return {
            "ok": True,
            "empty": False,
            "claim_token": "11111111-1111-4111-8111-111111111111",
            "media_id": "22222222-2222-4222-8222-222222222222",
            "media_asset_id": "33333333-3333-4333-8333-333333333333",
            "object_path": "linked_hd.jpg",
        }

    def read_private_image(self, object_path, *, max_bytes):
        self.read_args = (object_path, max_bytes)
        return b"\xff\xd8hd-image\xff\xd9"

    def complete_hd_review(self, claim_token, model_name, model_version, result):
        self.completed = (claim_token, model_name, model_version, result)
        return {"ok": True, "inserted": True}

    def fail_hd_review(self, claim_token, error_category):
        self.failed = (claim_token, error_category)
        return {"ok": True}


class FakeAnalyzer:
    def __init__(self, endpoint, model, deadline, clock):
        self.args = (endpoint, model, deadline)

    def analyze(self, image):
        return {
            "species": "whitetail",
            "sex": "male",
            "animal_count": 1,
            "identity_eligible": True,
            "age_eligible": True,
            "age_class": "mature_4_5_plus",
            "antler_score_eligible": False,
            "antler_score_range": "unknown",
            "distinguishing_features": ["split right brow tine"],
            "summary": "Mature whitetail buck; useful for identity.",
        }


class ReturnedHDWorkerTests(unittest.TestCase):
    def test_angle_aware_antler_and_age_result_is_normalized(self):
        result = {
            "species":"whitetail","sex":"male","animal_count":1,"identity_eligible":True,
            "view_angle":"broadside_left","head_visibility":"full","body_visibility":"full",
            "visible_tines_left":5,"visible_tines_right":2,"tine_count_limitations":"right side partly hidden by angle",
            "antler_structure":"wide main-beam sweep; tall G2/G3 on visible left side",
            "beam_observation":"left main beam visible from burr through tip","mass_observation":"moderate mass, strongest near bases",
            "spread_observation":"spread cannot be estimated reliably from broadside view","asymmetry_or_damage":"none visible",
            "antler_condition":"hard_antler","age_eligible":True,"age_class":"mature_4_5_plus",
            "age_cues":["deep chest","neck blends into shoulder","level back and slight belly sag"],
            "antler_score_eligible":False,"antler_score_range":"unknown","score_limitations":"no scale and no frontal view",
            "distinguishing_features":["split left brow tine"],"summary":"Broadside mature whitetail buck; five left-side and two right-side tines are visible, with the right obscured."
        }
        normalized = hd_analysis_worker.normalize_result(result)
        self.assertEqual(normalized["visible_tines_left"], 5)
        self.assertEqual(normalized["view_angle"], "broadside_left")
        self.assertIn("no frontal view", normalized["score_limitations"])

    def test_worker_claims_linked_hd_asset_and_records_profile_review_evidence(self):
        result = hd_analysis_worker.run_worker(
            {"SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key"},
            catalog_factory=FakeCatalog,
            analyzer_factory=FakeAnalyzer,
            deadline=100.0,
            clock=lambda: 0.0,
        )
        self.assertEqual(result, {"ok": True, "empty": False, "completed": 1, "failed": 0})
        catalog = FakeCatalog.instance
        self.assertEqual(catalog.claim_args, (hd_analysis_worker.MODEL_NAME, hd_analysis_worker.MODEL_VERSION))
        self.assertEqual(catalog.read_args[0], "linked_hd.jpg")
        self.assertEqual(catalog.completed[3]["identity_eligible"], True)
        self.assertEqual(catalog.completed[3]["distinguishing_features"], ["split right brow tine"])

    def test_model_failure_is_recorded_without_creating_review_result(self):
        class FailingAnalyzer(FakeAnalyzer):
            def analyze(self, image):
                raise hd_analysis_worker.ModelUnavailable("bad image")

        result = hd_analysis_worker.run_worker(
            {"SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key"},
            catalog_factory=FakeCatalog,
            analyzer_factory=FailingAnalyzer,
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(FakeCatalog.instance.failed[1], "model_unavailable")
        self.assertIsNone(FakeCatalog.instance.completed)


if __name__ == "__main__":
    unittest.main()
