import unittest
from pathlib import Path


class ManualProductionWorkflowTests(unittest.TestCase):
    def test_workflow_runs_regular_sweeps_and_uses_repository_secret(self):
        workflow = Path(".github/workflows/manual-production-sync.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("*/15 * * * *", workflow)
        self.assertIn("secrets.CRON_SECRET", workflow)
        self.assertIn("https://deer-re-id.vercel.app/api/sync", workflow)
        self.assertIn("?page=", workflow)
        self.assertIn("--retry 4", workflow)
        self.assertIn("--retry-all-errors", workflow)
        self.assertIn("page_count", workflow)
        self.assertNotIn("cron-secret", workflow.lower())


if __name__ == "__main__":
    unittest.main()
