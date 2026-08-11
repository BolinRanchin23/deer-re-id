import unittest

from reveal_downloader.gate1b import normalize_prediction, triage_prediction


class Gate1BTriageTests(unittest.TestCase):
    def test_visible_antler_routes_event_to_likely_male(self):
        prediction = normalize_prediction(
            {
                "animal_count": 2,
                "species": "whitetail",
                "visible_antler": "yes",
                "probable_male": "yes",
                "head_visibility": "partial",
                "lighting": "night_ir",
                "mixed_group": True,
                "all_animals_assessed": False,
                "reason": "One buck is visible; another deer is obscured.",
            }
        )
        self.assertEqual(triage_prediction(prediction), "likely_male")

    def test_unassessed_deer_never_becomes_female_candidate(self):
        prediction = normalize_prediction(
            {
                "animal_count": 1,
                "species": "axis",
                "visible_antler": "no",
                "probable_male": "no",
                "head_visibility": "partial",
                "lighting": "day_color",
                "mixed_group": False,
                "all_animals_assessed": False,
                "reason": "Head is partly hidden.",
            }
        )
        self.assertEqual(triage_prediction(prediction), "uncertain")

    def test_clear_target_deer_without_male_evidence_is_only_a_candidate_for_suppression(
        self,
    ):
        prediction = normalize_prediction(
            {
                "animal_count": 1,
                "species": "whitetail",
                "visible_antler": "no",
                "probable_male": "no",
                "head_visibility": "full",
                "lighting": "day_color",
                "mixed_group": False,
                "all_animals_assessed": True,
                "reason": "One clearly visible antlerless deer.",
            }
        )
        self.assertEqual(triage_prediction(prediction), "female_candidate")

    def test_zero_animal_output_can_never_be_a_female_candidate(self):
        prediction = normalize_prediction(
            {
                "animal_count": 0,
                "species": "axis",
                "visible_antler": "no",
                "probable_male": "no",
                "head_visibility": "full",
                "lighting": "day_color",
                "mixed_group": False,
                "all_animals_assessed": True,
                "reason": "No deer is visible.",
            }
        )
        self.assertEqual(triage_prediction(prediction), "uncertain")

    def test_non_target_and_malformed_outputs_fail_to_uncertain(self):
        non_target = normalize_prediction(
            {
                "animal_count": 1,
                "species": "non_deer",
                "visible_antler": "no",
                "probable_male": "no",
                "head_visibility": "full",
                "lighting": "day_color",
                "mixed_group": False,
                "all_animals_assessed": True,
                "reason": "A hog.",
            }
        )
        self.assertEqual(triage_prediction(non_target), "uncertain")
        with self.assertRaises(ValueError):
            normalize_prediction({"species": "whitetail"})

    def test_multiple_animals_require_mixed_group_and_unassessed_if_any_is_unclear(
        self,
    ):
        with self.assertRaises(ValueError):
            normalize_prediction(
                {
                    "animal_count": 2,
                    "species": "whitetail",
                    "visible_antler": "no",
                    "probable_male": "no",
                    "head_visibility": "full",
                    "lighting": "day_color",
                    "mixed_group": False,
                    "all_animals_assessed": True,
                    "reason": "Two animals.",
                }
            )


if __name__ == "__main__":
    unittest.main()
