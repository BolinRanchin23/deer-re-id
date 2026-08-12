import unittest
from unittest import mock
from scripts import run_gate1b_local

class Gate1BLocalRunnerTests(unittest.TestCase):
    def test_production_environment_uses_ephemeral_vercel_export(self):
        class Completed:
            stdout = ""
        def fake_run(command, **kwargs):
            destination = command[5]
            with open(destination, "w", encoding="utf-8") as handle:
                handle.write('OPENAI_API_KEY="test-key"\n')
            return Completed()
        with mock.patch.object(run_gate1b_local.subprocess, "run", side_effect=fake_run):
            values = run_gate1b_local._production_environment()
        self.assertEqual(values["OPENAI_API_KEY"], "test-key")

if __name__ == "__main__":
    unittest.main()
