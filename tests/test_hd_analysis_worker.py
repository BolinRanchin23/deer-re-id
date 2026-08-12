import unittest
from unittest import mock
from io import BytesIO
import math
from PIL import Image

from reveal_downloader import hd_analysis_worker


class FakeCatalog:
    instance = None

    def __init__(self, *_args):
        type(self).instance = self
        self.completed = None

    def set_deadline(self, deadline, *, clock):
        self.deadline = deadline

    def claim_hd_review(self, model_name, model_version, media_asset_id=None):
        self.claim_args = (model_name, model_version, media_asset_id)
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
    @staticmethod
    def crop_assessment():
        return {
            "species":"whitetail","sex":"male","identity_eligible":True,"view_angle":"unknown",
            "head_visibility":"full","body_visibility":"full","visible_tines_left":1,"visible_tines_right":1,
            "tine_count_limitations":"crop only","antler_structure":"crop only","beam_observation":"crop only",
            "mass_observation":"crop only","spread_observation":"crop only","asymmetry_or_damage":"none",
            "antler_condition":"hard_antler","age_eligible":False,"age_class":"unknown","age_cues":[],
            "antler_score_eligible":False,"antler_score_range":"unknown","score_limitations":"crop only",
            "distinguishing_features":["crop-specific"],"summary":"crop only"
        }

    def test_crop_padding_preserves_aspect_and_includes_head_body_margin(self):
        source = Image.new("RGB", (1000, 500), "white")
        encoded = BytesIO(); source.save(encoded, format="JPEG")
        box = {"x": 0.2, "y": 0.2, "width": 0.4, "height": 0.5}
        padded = hd_analysis_worker.padded_bbox(box)
        crop = Image.open(BytesIO(hd_analysis_worker.crop_animal_image(encoded.getvalue(), padded)))
        self.assertLess(padded["x"], box["x"])
        self.assertLess(padded["y"], box["y"])
        self.assertGreater(padded["width"], box["width"])
        self.assertGreater(padded["height"], box["height"])
        self.assertEqual(crop.size, (520, 350))

    def test_crop_padding_stays_inside_source_at_image_edges(self):
        upper=hd_analysis_worker.padded_bbox({"x":0.0,"y":0.0,"width":0.2,"height":0.3})
        lower=hd_analysis_worker.padded_bbox({"x":0.8,"y":0.7,"width":0.2,"height":0.3})
        for box in (upper,lower):
            self.assertGreater(box["width"],0)
            self.assertGreater(box["height"],0)
            self.assertLessEqual(box["x"]+box["width"],1.0)
            self.assertLessEqual(box["y"]+box["height"],1.0)

    def test_analyzer_rewrites_each_description_from_its_own_physical_crop(self):
        class CropAwareAnalyzer(hd_analysis_worker.OpenAIHDAnalyzer):
            def __init__(self): self.crops=[]
            def _request(self, image, prompt, schema, name, max_tokens):
                if name == "deerid_hd_localization":
                    return {"animal_count": 2, "detection_complete": True, "detection_notes": "two deer", "animals": [
                        {"instance_index": 1, "bbox": {"x": .05, "y": .1, "width": .35, "height": .7}},
                        {"instance_index": 2, "bbox": {"x": .55, "y": .1, "width": .35, "height": .7}},
                    ]}
                self.crops.append(image)
                return {**self.crop_result, "summary": f"crop-{len(image)}"}
        CropAwareAnalyzer.crop_result = {
            "species":"whitetail","sex":"male","identity_eligible":True,"view_angle":"unknown",
            "head_visibility":"full","body_visibility":"full","visible_tines_left":1,"visible_tines_right":1,
            "tine_count_limitations":"crop only","antler_structure":"crop only","beam_observation":"crop only",
            "mass_observation":"crop only","spread_observation":"crop only","asymmetry_or_damage":"none",
            "antler_condition":"hard_antler","age_eligible":False,"age_class":"unknown","age_cues":[],
            "antler_score_eligible":False,"antler_score_range":"unknown","score_limitations":"crop only",
            "distinguishing_features":["crop-specific"]
        }
        source=Image.new("RGB",(1000,500),"red");source.paste("blue",(500,0,1000,500));encoded=BytesIO();source.save(encoded,format="JPEG")
        analyzer=CropAwareAnalyzer();result=analyzer.analyze(encoded.getvalue())
        self.assertEqual(len(result["animals"]),2)
        self.assertEqual(result["animals"][0]["distinguishing_features"],["crop-specific"])
        self.assertNotEqual(result["animals"][0]["bbox"],result["animals"][1]["bbox"])
        left=Image.open(BytesIO(analyzer.crops[0])).resize((1,1)).getpixel((0,0))
        right=Image.open(BytesIO(analyzer.crops[1])).resize((1,1)).getpixel((0,0))
        self.assertGreater(left[0],left[2])
        self.assertGreater(right[2],right[0])

    def test_analyzer_reserves_deadline_across_localization_and_crop_requests(self):
        analyzer = hd_analysis_worker.OpenAIHDAnalyzer("key", hd_analysis_worker.DEFAULT_MODEL, deadline=100.0, clock=lambda: 95.0)
        with mock.patch.object(hd_analysis_worker.urllib.request, "urlopen") as opened, self.assertRaises(hd_analysis_worker.ModelUnavailable):
            analyzer._request(b"image", "prompt", {}, "test", 10)
        opened.assert_not_called()

    def test_analyzer_rejects_cross_boundary_localization_before_cropping(self):
        class InvalidBoxAnalyzer(hd_analysis_worker.OpenAIHDAnalyzer):
            def __init__(self): pass
            def _request(self, *_args):
                return {"animal_count":1,"detection_complete":True,"detection_notes":"one", "animals":[{"instance_index":1,"bbox":{"x":.9,"y":.1,"width":.5,"height":.5}}]}
        with mock.patch.object(hd_analysis_worker, "crop_animal_image") as crop, self.assertRaises(hd_analysis_worker.ModelUnavailable):
            InvalidBoxAnalyzer().analyze(b"image")
        crop.assert_not_called()

    def test_analyzer_rejects_non_finite_localization_before_cropping(self):
        for bad in (math.nan,math.inf,-math.inf):
            class NonFiniteAnalyzer(hd_analysis_worker.OpenAIHDAnalyzer):
                def __init__(self): pass
                def _request(self, *_args):
                    return {"animal_count":1,"detection_complete":True,"detection_notes":"one", "animals":[{"instance_index":1,"bbox":{"x":bad,"y":.1,"width":.4,"height":.5}}]}
            with self.subTest(value=bad), mock.patch.object(hd_analysis_worker, "crop_animal_image") as crop, self.assertRaises(hd_analysis_worker.ModelUnavailable):
                NonFiniteAnalyzer().analyze(b"image")
            crop.assert_not_called()

    def test_analyzer_rejects_duplicate_localization_boxes_before_narratives(self):
        class DuplicateBoxAnalyzer(hd_analysis_worker.OpenAIHDAnalyzer):
            def __init__(self): pass
            def _request(self, *_args):
                box={"x":.1,"y":.1,"width":.4,"height":.7}
                return {"animal_count":2,"detection_complete":True,"detection_notes":"two", "animals":[{"instance_index":1,"bbox":box},{"instance_index":2,"bbox":box}]}
        with mock.patch.object(hd_analysis_worker, "crop_animal_image") as crop, self.assertRaises(hd_analysis_worker.ModelUnavailable):
            DuplicateBoxAnalyzer().analyze(b"image")
        crop.assert_not_called()

    def test_analyzer_rejects_near_duplicate_localization_boxes(self):
        class NearDuplicateAnalyzer(hd_analysis_worker.OpenAIHDAnalyzer):
            def __init__(self): pass
            def _request(self, *_args):
                return {"animal_count":2,"detection_complete":True,"detection_notes":"two", "animals":[
                    {"instance_index":1,"bbox":{"x":.1,"y":.1,"width":.4,"height":.7}},
                    {"instance_index":2,"bbox":{"x":.105,"y":.105,"width":.4,"height":.7}},
                ]}
        with mock.patch.object(hd_analysis_worker, "crop_animal_image") as crop, self.assertRaises(hd_analysis_worker.ModelUnavailable):
            NearDuplicateAnalyzer().analyze(b"image")
        crop.assert_not_called()

    def test_malformed_intermediate_responses_fail_closed_as_model_unavailable(self):
        malformed = [
            {"animal_count":1,"detection_complete":True,"detection_notes":"one","animals":{}},
            {"animal_count":1,"detection_complete":True,"detection_notes":"one","animals":[None]},
        ]
        for response in malformed:
            class MalformedAnalyzer(hd_analysis_worker.OpenAIHDAnalyzer):
                def __init__(self): pass
                def _request(self, *_args): return response
            with self.subTest(response=response), self.assertRaises(hd_analysis_worker.ModelUnavailable):
                MalformedAnalyzer().analyze(b"image")

    def test_crop_assessment_cannot_override_trusted_index_or_bbox(self):
        class PoisonedAssessmentAnalyzer(hd_analysis_worker.OpenAIHDAnalyzer):
            calls=0
            def __init__(self): pass
            def _request(self, *_args):
                self.calls+=1
                if self.calls==1:
                    return {"animal_count":1,"detection_complete":True,"detection_notes":"one","animals":[{"instance_index":1,"bbox":{"x":.1,"y":.1,"width":.4,"height":.7}}]}
                return {**ReturnedHDWorkerTests.crop_assessment(),"instance_index":2}
        source=Image.new("RGB",(100,100),"white");encoded=BytesIO();source.save(encoded,format="JPEG")
        with self.assertRaises(hd_analysis_worker.ModelUnavailable):
            PoisonedAssessmentAnalyzer().analyze(encoded.getvalue())

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
        self.assertEqual(catalog.claim_args, (hd_analysis_worker.MODEL_NAME, hd_analysis_worker.MODEL_VERSION, None))
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

    def test_trigger_targets_the_new_hd_asset_and_requires_bearer_secret(self):
        environment = {"HD_ANALYSIS_TRIGGER_SECRET": "0123456789abcdef", "SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key", "OPENAI_API_KEY": "openai-test"}
        status, payload = hd_analysis_worker.handle_trigger(environment, "Bearer wrong", "33333333-3333-4333-8333-333333333333", catalog_factory=FakeCatalog, analyzer_factory=FakeAnalyzer)
        self.assertEqual(status, 401)
        status, payload = hd_analysis_worker.handle_trigger(environment, "Bearer 0123456789abcdef", "33333333-3333-4333-8333-333333333333", catalog_factory=FakeCatalog, analyzer_factory=FakeAnalyzer)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(FakeCatalog.instance.claim_args[2], "33333333-3333-4333-8333-333333333333")

    def test_serverless_trigger_bounds_multi_crop_analysis_to_runtime_deadline(self):
        environment = {"HD_ANALYSIS_TRIGGER_SECRET": "0123456789abcdef", "SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key", "OPENAI_API_KEY": "openai-test"}
        captured = {}
        def fake_worker(_environment, **kwargs):
            captured.update(kwargs); return {"ok": True, "empty": True, "completed": 0, "failed": 0}
        with mock.patch.object(hd_analysis_worker, "run_worker", side_effect=fake_worker):
            status, _ = hd_analysis_worker.handle_trigger(environment, "Bearer 0123456789abcdef", "33333333-3333-4333-8333-333333333333", clock=lambda: 100.0)
        self.assertEqual(status, 200)
        self.assertEqual(captured["deadline"], 370.0)


if __name__ == "__main__":
    unittest.main()
