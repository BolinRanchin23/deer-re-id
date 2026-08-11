"""Conservative Gate 1B prediction validation and event triage."""

from typing import Any, Mapping

SPECIES = {"whitetail", "axis", "other_deer", "non_deer", "unknown"}
TERNARY = {"yes", "no", "unknown"}
HEAD_VISIBILITY = {"full", "partial", "none", "unknown"}
LIGHTING = {"day_color", "night_ir", "unknown"}
TARGET_SPECIES = {"whitetail", "axis"}


def normalize_prediction(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one bounded VLM result without converting ambiguity to certainty."""
    if not isinstance(value, Mapping):
        raise ValueError("prediction must be an object")
    required = {
        "animal_count", "species", "visible_antler", "probable_male",
        "head_visibility", "lighting", "mixed_group", "all_animals_assessed", "reason",
    }
    if set(value) != required:
        raise ValueError("prediction fields are invalid")
    count = value["animal_count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 20:
        raise ValueError("animal_count is invalid")
    if value["species"] not in SPECIES:
        raise ValueError("species is invalid")
    for field in ("visible_antler", "probable_male"):
        if value[field] not in TERNARY:
            raise ValueError(f"{field} is invalid")
    if value["head_visibility"] not in HEAD_VISIBILITY:
        raise ValueError("head_visibility is invalid")
    if value["lighting"] not in LIGHTING:
        raise ValueError("lighting is invalid")
    if not isinstance(value["mixed_group"], bool) or not isinstance(value["all_animals_assessed"], bool):
        raise ValueError("prediction booleans are invalid")
    if count > 1 and not value["mixed_group"]:
        raise ValueError("multiple animals require mixed_group")
    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
        raise ValueError("reason is invalid")
    return {
        **value,
        "reason": reason.strip(),
    }


def triage_prediction(prediction: Mapping[str, Any]) -> str:
    """Return a prioritization class; suppression remains a separate validated policy."""
    if prediction.get("visible_antler") == "yes" or prediction.get("probable_male") == "yes":
        return "likely_male"
    if (
        prediction.get("species") in TARGET_SPECIES
        and prediction.get("visible_antler") == "no"
        and prediction.get("probable_male") == "no"
        and prediction.get("head_visibility") == "full"
        and prediction.get("all_animals_assessed") is True
    ):
        return "female_candidate"
    return "uncertain"
