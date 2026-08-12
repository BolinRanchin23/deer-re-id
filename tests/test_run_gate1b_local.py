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

if __name__ == "__main__":
    unittest.main()
