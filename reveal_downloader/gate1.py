"""Gate 1 event grouping and conservative photo routing."""

from datetime import datetime, timezone
import hashlib
from typing import Any, Iterable, Mapping

TARGET_SPECIES = {"white-tailed deer", "axis deer", "chital"}
EVENT_WINDOW_SECONDS = 5
ANIMAL_REVIEW_THRESHOLD = 0.05
CONFIDENT_SPECIES_THRESHOLD = 0.50


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_key(camera_id: str, first_media_id: str) -> str:
    return hashlib.sha256(f"{camera_id}:{first_media_id}".encode("utf-8")).hexdigest()[:24]


def _score(row: Mapping[str, Any]) -> tuple[float, float, str]:
    return (
        float(row.get("animal_confidence") or 0),
        float(row.get("animal_area") or 0),
        str(row.get("media_id") or ""),
    )


def _route(row: Mapping[str, Any]) -> tuple[str, str, int]:
    if row.get("model_failure"):
        return "review", "model_failure_abstain", 1
    species = str(row.get("species_label") or "").strip().lower()
    species_conf = float(row.get("species_confidence") or 0)
    animal_conf = float(row.get("animal_confidence") or 0)
    if species in TARGET_SPECIES and species_conf >= CONFIDENT_SPECIES_THRESHOLD:
        return "review", "target_species", 3
    if animal_conf >= ANIMAL_REVIEW_THRESHOLD and species_conf < CONFIDENT_SPECIES_THRESHOLD:
        return "review", "uncertain_animal", 2
    if animal_conf >= ANIMAL_REVIEW_THRESHOLD:
        return "archive", "confident_non_target", 0
    return "archive", "blank_or_below_threshold", 0


def _group_rows(normalized: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    precomputed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for row in normalized:
        key = str(row.get("event_key") or "")
        if key:
            precomputed.setdefault((str(row.get("camera_id") or ""), key), []).append(row)
        else:
            ungrouped.append(row)

    groups = list(precomputed.values())
    ungrouped.sort(key=lambda row: (str(row.get("camera_id", "")), _timestamp(str(row["captured_at"])), str(row.get("media_id", ""))))
    for row in ungrouped:
        if not groups or str(groups[-1][0].get("event_key") or ""):
            groups.append([row])
            continue
        prior = groups[-1][-1]
        same_camera = str(prior.get("camera_id")) == str(row.get("camera_id"))
        gap = (_timestamp(str(row["captured_at"])) - _timestamp(str(prior["captured_at"]))).total_seconds()
        if same_camera and 0 <= gap <= EVENT_WINDOW_SECONDS:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def route_events(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select at most one recall-biased review representative per camera event."""
    normalized = [dict(row) for row in rows]
    output: list[dict[str, Any]] = []
    for group in _group_rows(normalized):
        key = str(group[0].get("event_key") or _event_key(str(group[0].get("camera_id", "")), str(group[0].get("media_id", ""))))
        review_candidates = [row for row in group if _route(row)[0] == "review"]
        if review_candidates:
            representative = max(review_candidates, key=lambda row: (_route(row)[2],) + _score(row))
        else:
            representative = max(group, key=_score)
        route, reason, _ = _route(representative)
        for row in group:
            result = dict(row)
            result["event_key"] = key
            if row is representative:
                result.update(route=route, reason=reason, is_representative=True)
            else:
                result.update(route="event_duplicate", reason="lower_value_frame_in_event", is_representative=False)
            output.append(result)
    return output
