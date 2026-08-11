import unittest

from reveal_downloader.gate1 import route_events


class Gate1RoutingTests(unittest.TestCase):
    def test_selects_one_best_representative_per_five_second_camera_event(self):
        rows = [
            {"media_id": "a", "camera_id": "cam", "captured_at": "2026-08-11T00:00:00Z", "animal_confidence": 0.91, "animal_area": 0.20, "species_label": "white-tailed deer", "species_confidence": 0.95},
            {"media_id": "b", "camera_id": "cam", "captured_at": "2026-08-11T00:00:02Z", "animal_confidence": 0.96, "animal_area": 0.18, "species_label": "white-tailed deer", "species_confidence": 0.96},
            {"media_id": "c", "camera_id": "cam", "captured_at": "2026-08-11T00:00:20Z", "animal_confidence": 0.80, "animal_area": 0.30, "species_label": "white-tailed deer", "species_confidence": 0.90},
        ]
        result = {row["media_id"]: row for row in route_events(rows)}
        self.assertEqual(result["b"]["route"], "review")
        self.assertEqual(result["a"]["route"], "event_duplicate")
        self.assertEqual(result["c"]["route"], "review")
        self.assertEqual(result["a"]["event_key"], result["b"]["event_key"])
        self.assertNotEqual(result["b"]["event_key"], result["c"]["event_key"])

    def test_borderline_animal_abstains_to_review_and_blank_archives(self):
        rows = [
            {"media_id": "uncertain", "camera_id": "cam", "captured_at": "2026-08-11T00:00:00Z", "animal_confidence": 0.10, "animal_area": 0.01, "species_label": "", "species_confidence": 0.0},
            {"media_id": "blank", "camera_id": "cam", "captured_at": "2026-08-11T00:01:00Z", "animal_confidence": 0.01, "animal_area": 0.0, "species_label": "blank", "species_confidence": 0.99},
        ]
        result = {row["media_id"]: row for row in route_events(rows)}
        self.assertEqual(result["uncertain"]["route"], "review")
        self.assertEqual(result["uncertain"]["reason"], "uncertain_animal")
        self.assertEqual(result["blank"]["route"], "archive")

    def test_non_target_species_archives_when_confident(self):
        rows = [{"media_id": "hog", "camera_id": "cam", "captured_at": "2026-08-11T00:00:00Z", "animal_confidence": 0.97, "animal_area": 0.2, "species_label": "wild boar", "species_confidence": 0.93}]
        result = route_events(rows)
        self.assertEqual(result[0]["route"], "archive")
        self.assertEqual(result[0]["reason"], "confident_non_target")

    def test_target_species_frame_wins_over_higher_confidence_non_target_frame(self):
        rows = [
            {"media_id": "hog", "camera_id": "cam", "captured_at": "2026-08-11T00:00:00Z", "animal_confidence": 0.99, "animal_area": 0.4, "species_label": "wild pig", "species_confidence": 0.95},
            {"media_id": "deer", "camera_id": "cam", "captured_at": "2026-08-11T00:00:02Z", "animal_confidence": 0.70, "animal_area": 0.2, "species_label": "white-tailed deer", "species_confidence": 0.90},
        ]
        routed = {item["media_id"]: item for item in route_events(rows)}
        self.assertEqual(routed["deer"]["route"], "review")
        self.assertEqual(routed["hog"]["route"], "event_duplicate")

    def test_model_failure_abstains_to_review_instead_of_archiving_blank(self):
        rows = [{"media_id": "failed", "camera_id": "cam", "captured_at": "2026-08-11T00:00:00Z", "animal_confidence": 0.0, "animal_area": 0.0, "species_label": "", "species_confidence": 0.0, "model_failure": True}]
        routed = route_events(rows)
        self.assertEqual(routed[0]["route"], "review")
        self.assertEqual(routed[0]["reason"], "model_failure_abstain")

    def test_precomputed_event_key_is_used_across_batch_boundaries(self):
        rows = [
            {"media_id": "a", "camera_id": "cam", "captured_at": "2026-08-11T00:00:00Z", "event_key": "stable-event-1", "animal_confidence": 0.8, "animal_area": 0.2, "species_label": "white-tailed deer", "species_confidence": 0.8},
            {"media_id": "b", "camera_id": "cam", "captured_at": "2026-08-11T00:00:20Z", "event_key": "stable-event-1", "animal_confidence": 0.9, "animal_area": 0.2, "species_label": "white-tailed deer", "species_confidence": 0.8},
        ]
        routed = route_events(rows)
        self.assertEqual(sum(item["route"] == "review" for item in routed), 1)
        self.assertTrue(all(item["event_key"] == "stable-event-1" for item in routed))


if __name__ == "__main__":
    unittest.main()
