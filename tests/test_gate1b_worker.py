import json
import unittest

from reveal_downloader import gate1b_worker


class FakeCatalog:
    instance = None

    def __init__(self, *_args):
        type(self).instance = self
        self.recorded = None

    def set_deadline(self, deadline, *, clock):
        self.deadline = deadline

    def read_gate1b_pending(self, model_name, model_version, limit):
        self.pending_args = (model_name, model_version, limit)
        return [{
            "media_id": "11111111-1111-4111-8111-111111111111",
            "gate1_assessment_id": 17,
            "event_key": "stable-event-key",
            "camera_id": "22222222-2222-4222-8222-222222222222",
            "captured_at": "2026-08-11T00:00:00Z",
            "object_path": "private.jpg",
        }]

    def read_private_image(self, *_args, **_kwargs):
        return b"\xff\xd8image\xff\xd9"

    def record_gate1b_batch(self, model_name, model_version, results):
        self.recorded = (model_name, model_version, results)
        return {"ok": True, "inserted": len(results)}


class FakeAnalyzer:
    def __init__(self, endpoint, model, deadline, clock):
        self.args = (endpoint, model, deadline)

    def analyze(self, image):
        self.image = image
        return {
            "animal_count": 1,
            "species": "axis",
            "visible_antler": "yes",
            "probable_male": "yes",
            "head_visibility": "full",
            "lighting": "day_color",
            "mixed_group": False,
            "all_animals_assessed": True,
            "reason": "Visible axis stag with antlers.",
        }


class Gate1BWorkerTests(unittest.TestCase):
    def test_worker_records_append_only_versioned_prediction_and_triage(self):
        result = gate1b_worker.run_worker(
            {"SUPABASE_URL": "https://project.supabase.co", "SUPABASE_SECRET_KEY": "secret"},
            limit=4,
            catalog_factory=FakeCatalog,
            analyzer_factory=FakeAnalyzer,
            deadline=100.0,
            clock=lambda: 0.0,
        )
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(result["likely_male"], 1)
        catalog = FakeCatalog.instance
        self.assertEqual(catalog.pending_args, (gate1b_worker.MODEL_NAME, gate1b_worker.MODEL_VERSION, 4))
        model_name, model_version, rows = catalog.recorded
        self.assertEqual((model_name, model_version), (gate1b_worker.MODEL_NAME, gate1b_worker.MODEL_VERSION))
        self.assertEqual(rows[0]["triage_class"], "likely_male")
        self.assertEqual(rows[0]["species_label"], "axis")
        self.assertEqual(rows[0]["gate1_assessment_id"], 17)

    def test_model_failure_is_not_persisted_as_a_model_prediction_and_remains_retryable(self):
        class FailingAnalyzer(FakeAnalyzer):
            def analyze(self, image):
                raise gate1b_worker.ModelUnavailable("bad response")

        result = gate1b_worker.run_worker(
            {"SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key"},
            catalog_factory=FakeCatalog,
            analyzer_factory=FailingAnalyzer,
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["recorded"], 0)
        self.assertIsNone(FakeCatalog.instance.recorded)

    def test_ollama_client_requires_loopback_and_validates_structured_response(self):
        with self.assertRaises(ValueError):
            gate1b_worker.OllamaVisionClient("http://example.com:11434", "model")

        response = json.dumps({"response": json.dumps({
            "animal_count": 1,
            "species": "whitetail",
            "visible_antler": "no",
            "probable_male": "no",
            "head_visibility": "full",
            "lighting": "night_ir",
            "mixed_group": False,
            "all_animals_assessed": True,
            "reason": "One clear antlerless deer.",
        }), "done": True}).encode()

        class Transport:
            def request(self, method, url, *, headers, body, timeout, max_response_bytes):
                self.body = json.loads(body)
                return 200, response

        transport = Transport()
        client = gate1b_worker.OllamaVisionClient(
            "http://127.0.0.1:11434", "gemma4:e4b", transport=transport
        )
        prediction = client.analyze(b"jpeg")
        self.assertEqual(prediction["species"], "whitetail")
        self.assertEqual(transport.body["options"]["temperature"], 0)
        self.assertFalse(transport.body["stream"])


if __name__ == "__main__":
    unittest.main()
