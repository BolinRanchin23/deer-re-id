import unittest
from pathlib import Path


class ManualProductionWorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_only_and_uses_repository_secret(self):
        workflow = Path(".github/workflows/manual-production-sync.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("secrets.CRON_SECRET", workflow)
        self.assertIn("https://deer-re-id.vercel.app/api/sync", workflow)
        self.assertNotIn("cron-secret", workflow.lower())


if __name__ == "__main__":
    unittest.main()
