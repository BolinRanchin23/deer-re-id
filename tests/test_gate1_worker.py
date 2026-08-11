import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reveal_downloader import gate1_worker


class FakeCatalog:
    instance = None

    def __init__(self, *_args):
        FakeCatalog.instance = self
        self.recorded = None

    def read_gate1_pending(self, *_args):
        return [{
            "media_id": "11111111-1111-4111-8111-111111111111",
            "camera_id": "22222222-2222-4222-8222-222222222222",
            "captured_at": "2026-08-11T00:00:00Z",
            "event_key": "stable-event-key",
            "object_path": "private.jpg",
        }]

    def read_private_image(self, *_args, **_kwargs):
        return b"fake-jpeg"

    def record_gate1_batch(self, model_name, model_version, results):
        self.recorded = (model_name, model_version, results)
        return {"ok": True, "inserted": len(results)}


class Gate1WorkerTests(unittest.TestCase):
    def test_worker_uses_supported_speciesnet_flags_and_preserves_failures_as_abstentions(self):
        observed_command = None

        def fake_run(command, **_kwargs):
            nonlocal observed_command
            observed_command = command
            folder = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--folders=")))
            output = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--predictions_json=")))
            image = next(folder.iterdir())
            output.write_text(json.dumps({"predictions": [{
                "filepath": str(image), "prediction": "", "prediction_score": 0,
                "detections": [], "failures": ["DETECTOR"], "model_version": "4.0.3a",
            }]}))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        env = {"SUPABASE_URL": "https://project.supabase.co", "SUPABASE_SECRET_KEY": "secret"}
        with patch.object(gate1_worker, "SupabaseCatalog", FakeCatalog), patch.object(gate1_worker.subprocess, "run", fake_run):
            result = gate1_worker.run_worker(env, limit=1)

        self.assertIsNotNone(observed_command)
        assert observed_command is not None
        self.assertNotIn("--new_file", observed_command)
        self.assertEqual(result["review"], 1)
        self.assertIsNotNone(FakeCatalog.instance)
        assert FakeCatalog.instance is not None and FakeCatalog.instance.recorded is not None
        recorded = FakeCatalog.instance.recorded[2][0]
        self.assertEqual(recorded["event_key"], "stable-event-key")
        self.assertEqual(recorded["route"], "review")
        self.assertEqual(recorded["reason"], "model_failure_abstain")


if __name__ == "__main__":
    unittest.main()
