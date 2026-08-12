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
    def __init__(self, api_key, model, deadline, clock):
        self.args = (api_key, model, deadline)

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
    def test_multi_animal_result_preserves_one_bbox_and_analysis_per_deer(self):
        animal = {
            "instance_index": 1,
            "bbox": {"x": 0.08, "y": 0.14, "width": 0.34, "height": 0.72},
            "species":"whitetail","sex":"male","identity_eligible":True,
            "view_angle":"broadside_left","head_visibility":"full","body_visibility":"full",
            "visible_tines_left":5,"visible_tines_right":2,"tine_count_limitations":"right side partly hidden",
            "antler_structure":"wide sweep","beam_observation":"left beam visible","mass_observation":"moderate",
            "spread_observation":"not reliable broadside","asymmetry_or_damage":"none visible",
            "antler_condition":"hard_antler","age_eligible":True,"age_class":"mature_4_5_plus",
            "age_cues":["deep chest"],"antler_score_eligible":False,"antler_score_range":"unknown",
            "score_limitations":"no scale","distinguishing_features":["split brow"],"summary":"Left buck."
        }
        result = hd_analysis_worker.normalize_result({
            "animal_count": 2,
            "detection_complete": True,
            "detection_notes": "two separated bucks",
            "animals": [animal, {**animal, "instance_index": 2, "bbox": {"x": 0.54, "y": 0.2, "width": 0.38, "height": 0.68}, "summary": "Right buck."}],
        })
        self.assertEqual(result["animal_count"], 2)
        self.assertEqual([x["instance_index"] for x in result["animals"]], [1, 2])
        self.assertEqual(result["animals"][1]["bbox"]["x"], 0.54)

    def test_multi_animal_result_rejects_duplicate_indexes_and_unsafe_boxes(self):
        base = {
            "instance_index": 1, "bbox": {"x": 0.8, "y": 0.1, "width": 0.3, "height": 0.5},
            "species":"whitetail","sex":"male","identity_eligible":True,"view_angle":"unknown",
            "head_visibility":"partial","body_visibility":"partial","visible_tines_left":0,"visible_tines_right":0,
            "tine_count_limitations":"unknown","antler_structure":"unknown","beam_observation":"unknown",
            "mass_observation":"unknown","spread_observation":"unknown","asymmetry_or_damage":"unknown",
            "antler_condition":"unknown","age_eligible":False,"age_class":"unknown","age_cues":[],
            "antler_score_eligible":False,"antler_score_range":"unknown","score_limitations":"unknown",
            "distinguishing_features":[],"summary":"Partial deer."
        }
        with self.assertRaises(hd_analysis_worker.ModelUnavailable):
            hd_analysis_worker.normalize_result({"animal_count":1,"detection_complete":True,"detection_notes":"one","animals":[base]})

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
            {"SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key", "OPENAI_API_KEY": "openai-test"},
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
            {"SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key", "OPENAI_API_KEY": "openai-test"},
            catalog_factory=FakeCatalog,
            analyzer_factory=FailingAnalyzer,
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(FakeCatalog.instance.failed[1], "model_unavailable")
        self.assertIsNone(FakeCatalog.instance.completed)


if __name__ == "__main__":
    unittest.main()
