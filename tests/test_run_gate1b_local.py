import unittest

from scripts import run_gate1b_local


class Gate1BLocalRunnerTests(unittest.TestCase):
    def test_model_tag_requires_exact_pinned_digest(self):
        self.assertFalse(
            run_gate1b_local.has_pinned_model(
                {"models": [{"name": run_gate1b_local.MODEL, "digest": "0" * 64}]}
            )
        )
        self.assertTrue(
            run_gate1b_local.has_pinned_model(
                {
                    "models": [
                        {
                            "name": run_gate1b_local.MODEL,
                            "digest": run_gate1b_local.MODEL_DIGEST,
                        }
                    ]
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
