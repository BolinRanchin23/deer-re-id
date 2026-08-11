import json
import os
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
        self.released_claim = None

    def set_deadline(self, deadline, *, clock):
        self.deadline = deadline
        self.clock = clock

    def read_gate1_pending(self, *_args):
        return [{
            "media_id": "11111111-1111-4111-8111-111111111111",
            "camera_id": "22222222-2222-4222-8222-222222222222",
            "captured_at": "2026-08-11T00:00:00Z",
            "event_key": "stable-event-key",
            "object_path": "private.jpg",
            "claim_token": "33333333-3333-4333-8333-333333333333",
        }]

    def read_private_image(self, *_args, **_kwargs):
        return b"fake-jpeg"

    def record_gate1_batch(self, model_name, model_version, claim_token, results):
        self.recorded = (model_name, model_version, claim_token, results)
        return {"ok": True, "inserted": len(results), "released": 1}

    def release_gate1_claim(self, claim_token):
        self.released_claim = claim_token
        return {"ok": True, "released": 1}


class Gate1WorkerTests(unittest.TestCase):
    def test_worker_uses_supported_speciesnet_flags_and_preserves_failures_as_abstentions(self):
        observed_command = None
        observed_child_env = None

        def fake_run(command, **kwargs):
            nonlocal observed_command, observed_child_env
            observed_command = command
            observed_child_env = kwargs["env"]
            folder = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--folders=")))
            output = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--predictions_json=")))
            image = next(folder.iterdir())
            output.write_text(json.dumps({"predictions": [{
                "filepath": str(image), "prediction": "", "prediction_score": 0,
                "detections": [], "failures": ["DETECTOR"], "model_version": "4.0.3a",
            }]}))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        env = {"SUPABASE_URL": "https://project.supabase.co", "SUPABASE_SECRET_KEY": "secret"}
        with patch.dict(os.environ, {
            "SUPABASE_SECRET_KEY": "must-not-leak",
            "SUPABASE_URL": "https://must-not-leak.example",
        }), patch.object(gate1_worker, "SupabaseCatalog", FakeCatalog), patch.object(gate1_worker.subprocess, "run", fake_run):
            result = gate1_worker.run_worker(env, limit=1)

        self.assertIsNotNone(observed_command)
        assert observed_command is not None
        self.assertNotIn("--new_file", observed_command)
        self.assertIsNotNone(observed_child_env)
        assert observed_child_env is not None
        self.assertNotIn("SUPABASE_SECRET_KEY", observed_child_env)
        self.assertNotIn("SUPABASE_URL", observed_child_env)
        self.assertEqual(result["review"], 1)
        self.assertIsNotNone(FakeCatalog.instance)
        assert FakeCatalog.instance is not None and FakeCatalog.instance.recorded is not None
        self.assertEqual(FakeCatalog.instance.recorded[2], "33333333-3333-4333-8333-333333333333")
        recorded = FakeCatalog.instance.recorded[3][0]
        self.assertEqual(recorded["event_key"], "stable-event-key")
        self.assertEqual(recorded["route"], "review")
        self.assertEqual(recorded["reason"], "model_failure_abstain")
        self.assertIsNone(FakeCatalog.instance.released_claim)

    def test_catchup_repeats_batches_until_reserve_time_is_reached(self):
        now = [0.0]
        pending_batches = [
            {"ok": True, "pending": 77, "recorded": 77, "review": 5, "archived": 50, "event_duplicates": 22},
            {"ok": True, "pending": 55, "recorded": 55, "review": 4, "archived": 45, "event_duplicates": 6},
            {"ok": True, "pending": 0, "recorded": 0, "review": 0},
        ]

        observed_deadlines = []

        def fake_batch(_environ, *, limit, deadline, clock):
            self.assertEqual(limit, 50)
            observed_deadlines.append(deadline)
            self.assertEqual(clock(), now[0])
            now[0] += 300
            return pending_batches.pop(0)

        result = gate1_worker.run_catchup(
            {}, limit=50, time_budget_seconds=720, reserve_seconds=300,
            clock=lambda: now[0], run_batch=fake_batch,
        )

        self.assertEqual(result["stop_reason"], "time_budget")
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["recorded"], 132)
        self.assertEqual(result["review"], 9)
        self.assertEqual(result["archived"], 95)
        self.assertEqual(result["event_duplicates"], 28)
        self.assertEqual(len(pending_batches), 1)
        self.assertEqual(observed_deadlines, [720.0, 720.0])

    def test_worker_caps_speciesnet_to_the_absolute_catchup_deadline(self):
        observed_timeout = None

        def fake_run(command, **kwargs):
            nonlocal observed_timeout
            observed_timeout = kwargs["timeout"]
            folder = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--folders=")))
            output = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--predictions_json=")))
            image = next(folder.iterdir())
            output.write_text(json.dumps({"predictions": [{
                "filepath": str(image), "prediction": "blank", "prediction_score": 1,
                "detections": [], "failures": [], "model_version": "4.0.3a",
            }]}))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        env = {"SUPABASE_URL": "https://project.supabase.co", "SUPABASE_SECRET_KEY": "secret"}
        with patch.object(gate1_worker, "SupabaseCatalog", FakeCatalog), patch.object(gate1_worker.subprocess, "run", fake_run):
            gate1_worker.run_worker(env, limit=1, deadline=120.0, clock=lambda: 20.0)

        self.assertEqual(observed_timeout, 100.0)

    def test_catchup_stops_cleanly_when_an_inflight_batch_reaches_deadline(self):
        def timed_out_batch(_environ, **_kwargs):
            raise gate1_worker.subprocess.TimeoutExpired(["speciesnet"], 120)

        result = gate1_worker.run_catchup(
            {}, time_budget_seconds=720, clock=lambda: 0.0,
            run_batch=timed_out_batch,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["stop_reason"], "time_budget")
        self.assertEqual(result["batches"], 0)

    def test_production_workflow_chains_after_ingestion_and_runs_twelve_minute_catchup(self):
        workflow = Path(".github/workflows/gate1-production.yml").read_text()
        self.assertIn("workflow_run:", workflow)
        self.assertIn('workflows: ["Reveal Production Sync"]', workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("branches: [main]", workflow)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", workflow)
        self.assertIn("--until-empty", workflow)
        self.assertIn("--time-budget-seconds 720", workflow)
        self.assertIn("timeout-minutes: 25", workflow)
        job_env = workflow.split("    steps:", 1)[0]
        run_step = workflow.split("- name: Catch up production triage", 1)[1]
        self.assertNotIn("SUPABASE_SECRET_KEY", job_env)
        self.assertIn("SUPABASE_SECRET_KEY: ${{ secrets.SUPABASE_SECRET_KEY }}", run_step)

    def test_cli_enables_bounded_catchup_mode(self):
        observed = {}

        def fake_catchup(_environ, **kwargs):
            observed.update(kwargs)
            return {"ok": True, "batches": 0, "stop_reason": "queue_empty"}

        argv = [
            "gate1_worker", "--limit", "50", "--until-empty",
            "--time-budget-seconds", "720",
        ]
        with patch.object(gate1_worker, "run_catchup", fake_catchup), patch.object(gate1_worker.sys, "argv", argv), patch("builtins.print"):
            gate1_worker.main()

        self.assertEqual(observed["limit"], 50)
        self.assertEqual(observed["time_budget_seconds"], 720)


if __name__ == "__main__":
    unittest.main()
