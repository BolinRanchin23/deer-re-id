import json
import unittest
from unittest import mock
from pathlib import Path

from reveal_downloader import gate1b_worker


class FakeCatalog:
    instance = None

    def __init__(self, *_args):
        type(self).instance = self
        self.recorded = None

    def set_deadline(self, deadline, *, clock):
        self.deadline = deadline

    def claim_gate1b_batch(self, model_name, model_version, limit):
        self.pending_args = (model_name, model_version, limit)
        items=[
            {
                "media_id": "11111111-1111-4111-8111-111111111111",
                "gate1_assessment_id": 17,
                "event_key": "stable-event-key",
                "camera_id": "22222222-2222-4222-8222-222222222222",
                "captured_at": "2026-08-11T00:00:00Z",
                "object_path": "private.jpg",
            }
        ]
        self.claimed_count=len(items)
        return {"ok":True,"empty":False,"claim_token":"33333333-3333-4333-8333-333333333333","items":items}

    def read_private_image(self, *_args, **_kwargs):
        return b"\xff\xd8image\xff\xd9"

    def complete_gate1b_batch(self, claim_token, model_name, model_version, results):
        self.recorded = (model_name, model_version, results)
        self.claim_token=claim_token
        return {"ok":True,"claimed":self.claimed_count,"persisted":len(results),"unfinished":self.claimed_count-len(results)}


class FakeAnalyzer:
    instance = None

    def __init__(self, api_key, model, deadline, clock):
        type(self).instance = self
        self.args = (api_key, model, deadline)

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
    def test_vercel_cron_requires_auth_and_runs_bounded_worker(self):
        environ={"CRON_SECRET":"cron-secret-at-least-16"}
        self.assertEqual(gate1b_worker.handle_cron(environ,None)[0],401)
        with mock.patch.object(gate1b_worker,"run_worker",return_value={"ok":True,"pending":1,"recorded":1,"failed":0}) as run:
            status,payload=gate1b_worker.handle_cron(environ,"Bearer cron-secret-at-least-16",clock=lambda:100.0)
        self.assertEqual((status,payload["ok"]),(200,True))
        self.assertEqual(run.call_args.kwargs["limit"],10)
        self.assertEqual(run.call_args.kwargs["deadline"],340.0)

    def test_worker_stops_before_deadline_and_records_completed_rows(self):
        class TwoCatalog(FakeCatalog):
            def claim_gate1b_batch(self,*_args):
                row={"media_id":"11111111-1111-4111-8111-111111111111","gate1_assessment_id":17,"event_key":"one","object_path":"one.jpg"}
                self.claimed_count=2
                return {"ok":True,"empty":False,"claim_token":"33333333-3333-4333-8333-333333333333","items":[row,{**row,"media_id":"22222222-2222-4222-8222-222222222222","gate1_assessment_id":18,"event_key":"two","object_path":"two.jpg"}]}
        ticks=iter([0.0,75.0])
        result=gate1b_worker.run_worker(
            {"SUPABASE_URL":"url","SUPABASE_SECRET_KEY":"key","OPENAI_API_KEY":"openai"},
            catalog_factory=TwoCatalog,analyzer_factory=FakeAnalyzer,deadline=100.0,clock=lambda:next(ticks),
        )
        self.assertEqual((result["recorded"],result["failed"]),(1,1))

    def test_worker_rejects_partial_durable_completion(self):
        class PartialCatalog(FakeCatalog):
            def complete_gate1b_batch(self,*_args):
                return {"ok":True,"claimed":1,"persisted":0,"unfinished":1}
        with self.assertRaises(RuntimeError):
            gate1b_worker.run_worker({"SUPABASE_URL":"url","SUPABASE_SECRET_KEY":"key","OPENAI_API_KEY":"openai"},catalog_factory=PartialCatalog,analyzer_factory=FakeAnalyzer)

    def test_vercel_cron_reports_semantic_model_failure(self):
        with mock.patch.object(gate1b_worker,"run_worker",return_value={"ok":True,"pending":1,"recorded":0,"failed":1}):
            status,payload=gate1b_worker.handle_cron({"CRON_SECRET":"cron-secret-at-least-16"},"Bearer cron-secret-at-least-16")
        self.assertEqual(status,503)
        self.assertEqual(payload,{"ok":False,"error":"Gate 1B batch incomplete","failed":1})

    def test_vercel_cron_submits_queued_hd_after_successful_gate1b(self):
        environ={
            "CRON_SECRET":"cron-secret-at-least-16",
            "TACTACAM_USERNAME":"user","TACTACAM_PASSWORD":"pass",
            "SUPABASE_URL":"url","SUPABASE_SECRET_KEY":"key",
        }
        gate1b={"ok":True,"pending":1,"recorded":1,"failed":0,"likely_male":1}
        hd={"ok":True,"submitted":1,"failed":0,"unknown":0,"empty":True}
        with mock.patch.object(gate1b_worker,"run_worker",return_value=gate1b), mock.patch.object(
            gate1b_worker,"run_hd_request_worker",return_value=hd
        ) as drain:
            status,payload=gate1b_worker.handle_cron(environ,"Bearer cron-secret-at-least-16",clock=lambda:100.0)
        self.assertEqual(status,200)
        self.assertEqual(payload["hd_requests"],hd)
        self.assertEqual(drain.call_args.kwargs["max_requests"],20)

    def test_vercel_cron_drains_existing_hd_queue_even_when_gate1b_is_empty(self):
        environ={
            "CRON_SECRET":"cron-secret-at-least-16",
            "TACTACAM_USERNAME":"user","TACTACAM_PASSWORD":"pass",
            "SUPABASE_URL":"url","SUPABASE_SECRET_KEY":"key",
        }
        gate1b={"ok":True,"pending":0,"recorded":0,"failed":0,"likely_male":0}
        hd={"ok":True,"submitted":1,"failed":0,"unknown":0,"empty":True}
        with mock.patch.object(gate1b_worker,"run_worker",return_value=gate1b), mock.patch.object(
            gate1b_worker,"run_hd_request_worker",return_value=hd
        ):
            status,payload=gate1b_worker.handle_cron(environ,"Bearer cron-secret-at-least-16")
        self.assertEqual(status,200)
        self.assertEqual(payload["hd_requests"]["submitted"],1)

    def test_vercel_config_schedules_gate1b_and_bounds_runtime(self):
        root=Path(__file__).parents[1]
        config=json.loads((root/"vercel.json").read_text())
        self.assertEqual(config["functions"]["api/gate1b_cron.py"]["maxDuration"],300)
        self.assertIn({"path":"/api/gate1b_cron","schedule":"*/15 * * * *"},config["crons"])
        self.assertTrue((root/"api/gate1b_cron.py").is_file())

    def test_worker_records_append_only_versioned_prediction_and_triage(self):
        result = gate1b_worker.run_worker(
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "secret",
                "OPENAI_API_KEY": "test-openai-key",
            },
            limit=4,
            catalog_factory=FakeCatalog,
            analyzer_factory=FakeAnalyzer,
            deadline=100.0,
            clock=lambda: 0.0,
        )
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(result["likely_male"], 1)
        catalog = FakeCatalog.instance
        self.assertEqual(catalog.pending_args,(gate1b_worker.MODEL_NAME,gate1b_worker.MODEL_VERSION,4))
        self.assertEqual(catalog.claim_token,"33333333-3333-4333-8333-333333333333")
        model_name, model_version, rows = catalog.recorded
        self.assertEqual(
            (model_name, model_version),
            (gate1b_worker.MODEL_NAME, gate1b_worker.MODEL_VERSION),
        )
        self.assertEqual(rows[0]["triage_class"], "likely_male")
        self.assertEqual(rows[0]["species_label"], "axis")
        self.assertEqual(rows[0]["gate1_assessment_id"], 17)
        analyzer = FakeAnalyzer.instance
        self.assertIsNotNone(analyzer)
        self.assertEqual(analyzer.args[1], gate1b_worker.DEFAULT_MODEL)

    def test_model_failure_is_not_persisted_as_a_model_prediction_and_remains_retryable(
        self,
    ):
        class FailingAnalyzer(FakeAnalyzer):
            def analyze(self, image):
                raise gate1b_worker.ModelUnavailable("bad response")

        result = gate1b_worker.run_worker(
            {"SUPABASE_URL": "url", "SUPABASE_SECRET_KEY": "key", "OPENAI_API_KEY": "test-openai-key"},
            catalog_factory=FakeCatalog,
            analyzer_factory=FailingAnalyzer,
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(FakeCatalog.instance.recorded[2],[])

    def test_openai_client_uses_pinned_snapshot_and_validates_structured_response(self):
        response = json.dumps(
            {
                "choices": [{"message": {"content": json.dumps({
                        "animal_count": 1,
                        "species": "whitetail",
                        "visible_antler": "no",
                        "probable_male": "no",
                        "head_visibility": "full",
                        "lighting": "night_ir",
                        "mixed_group": False,
                        "all_animals_assessed": True,
                        "reason": "One clear antlerless deer.",
                    })}}],
            }
        ).encode()

        class Transport:
            def request(
                self, method, url, *, headers, body, timeout, max_response_bytes
            ):
                self.headers = headers
                self.body = json.loads(body)
                return 200, response

        transport = Transport()
        client = gate1b_worker.OpenAIVisionClient(
            "test-openai-key", gate1b_worker.DEFAULT_MODEL, transport=transport
        )
        prediction = client.analyze(b"jpeg")
        self.assertEqual(prediction["species"], "whitetail")
        self.assertEqual(transport.body["model"], "gpt-4o-mini-2024-07-18")
        self.assertEqual(transport.body["temperature"], 0)
        self.assertEqual(transport.body["response_format"]["type"], "json_schema")
        self.assertEqual(transport.headers["Authorization"], "Bearer test-openai-key")

    def test_openai_client_rejects_missing_key_and_mutable_model_alias(self):
        with self.assertRaises(ValueError):
            gate1b_worker.OpenAIVisionClient("", gate1b_worker.DEFAULT_MODEL)
        with self.assertRaises(ValueError):
            gate1b_worker.OpenAIVisionClient("test", "gpt-4o-mini")


if __name__ == "__main__":
    unittest.main()
