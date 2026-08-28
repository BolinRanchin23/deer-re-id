import unittest
from unittest import mock
from scripts import run_gate1b_local

class Gate1BLocalRunnerTests(unittest.TestCase):
    def test_bitwarden_secret_is_resolved_without_logging_value(self):
        class Completed:
            stdout = ""
        def fake_run(command, **kwargs):
            Completed.stdout = '[{"key":"OPENAI_API_KEY","value":"test-key"}]'
            return Completed()
        with mock.patch.object(run_gate1b_local.subprocess, "run", side_effect=fake_run):
            value = run_gate1b_local._bitwarden_openai_key()
        self.assertEqual(value, "test-key")

    def test_bitwarden_resolution_retries_one_transient_cli_failure(self):
        class Completed:
            stdout = '[{"key":"OPENAI_API_KEY","value":"test-key"}]'
        attempts = []
        def fake_run(command, **kwargs):
            attempts.append(command)
            if len(attempts) == 1:
                raise run_gate1b_local.subprocess.CalledProcessError(1, command)
            return Completed()
        with mock.patch.object(run_gate1b_local.subprocess, "run", side_effect=fake_run), mock.patch.object(run_gate1b_local.time, "sleep"):
            value = run_gate1b_local._bitwarden_openai_key()
        self.assertEqual(value, "test-key")
        self.assertEqual(len(attempts), 2)

if __name__ == "__main__":
    unittest.main()
