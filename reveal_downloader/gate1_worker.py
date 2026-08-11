"""Production batch worker for DeerID Gate 1 using public SpeciesNet weights."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping

from .catalog import SupabaseCatalog
from .gate1 import route_events

MODEL_NAME = "SpeciesNet"
MODEL_VERSION = "4.0.3a"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
RECORDING_RESERVE_SECONDS = 30.0
SPECIESNET_ENV_ALLOWLIST = {
    "HOME", "KAGGLEHUB_CACHE", "LANG", "LC_ALL", "PATH",
    "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "TMP", "TEMP", "TMPDIR",
    "XDG_CACHE_HOME",
}


def _assessment(prediction: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    raw_detections = prediction.get("detections")
    detections = raw_detections if isinstance(raw_detections, list) else []
    animal_detections = [
        item for item in detections
        if isinstance(item, Mapping) and str(item.get("label") or item.get("category")) in {"animal", "1"}
    ]
    animal_confidence = max((float(item.get("conf") or 0) for item in animal_detections), default=0.0)
    animal_area = max(
        (
            float(item.get("bbox", [0, 0, 0, 0])[2]) * float(item.get("bbox", [0, 0, 0, 0])[3])
            for item in animal_detections
            if isinstance(item.get("bbox"), list) and len(item["bbox"]) == 4
        ),
        default=0.0,
    )
    taxonomy = str(prediction.get("prediction") or "")
    species_label = taxonomy.rsplit(";", 1)[-1].strip().lower() if taxonomy else ""
    return {
        "media_id": metadata["media_id"],
        "camera_id": metadata["camera_id"],
        "captured_at": metadata["captured_at"],
        "event_key": metadata.get("event_key"),
        "animal_confidence": min(1.0, max(0.0, animal_confidence)),
        "animal_area": min(1.0, max(0.0, animal_area)),
        "species_label": species_label,
        "species_confidence": min(1.0, max(0.0, float(prediction.get("prediction_score") or 0))),
        "model_failure": bool(prediction.get("failures")),
        "detections": [dict(item) for item in animal_detections[:50]],
        "raw_output": {
            "prediction": taxonomy,
            "prediction_source": prediction.get("prediction_source"),
            "classifications": prediction.get("classifications"),
            "model_version": prediction.get("model_version"),
            "failures": prediction.get("failures"),
        },
    }


def run_worker(
    environ: Mapping[str, str],
    *,
    limit: int = 60,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    url = environ.get("SUPABASE_URL", "")
    key = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
    catalog = SupabaseCatalog(url, key, environ.get("SUPABASE_BUCKET", "tactacam-photos"))
    if deadline is not None:
        catalog.set_deadline(deadline, clock=clock)
    pending = catalog.read_gate1_pending(MODEL_NAME, MODEL_VERSION, limit)
    if not isinstance(pending, list):
        raise RuntimeError("Gate 1 pending RPC returned an invalid payload")
    if not pending:
        return {"ok": True, "pending": 0, "recorded": 0, "review": 0}
    claim_tokens = {
        str(item.get("claim_token") or "")
        for item in pending
        if isinstance(item, Mapping)
    }
    if len(claim_tokens) != 1 or "" in claim_tokens:
        raise RuntimeError("Gate 1 pending batch did not contain one valid claim token")
    claim_token = claim_tokens.pop()

    with tempfile.TemporaryDirectory(prefix="deerid-gate1-") as tmp:
        image_dir = Path(tmp) / "images"
        image_dir.mkdir()
        by_path: dict[str, Mapping[str, Any]] = {}
        for item in pending:
            if not isinstance(item, Mapping):
                raise RuntimeError("Gate 1 pending item is invalid")
            object_path = str(item.get("object_path") or "")
            suffix = ".png" if object_path.endswith(".png") else ".jpg"
            local = image_dir / f"{item['media_id']}{suffix}"
            local.write_bytes(catalog.read_private_image(object_path, max_bytes=MAX_IMAGE_BYTES))
            by_path[str(local.resolve())] = item

        output_path = Path(tmp) / "speciesnet.json"
        command = [
            sys.executable, "-m", "speciesnet.scripts.run_model",
            f"--folders={image_dir}", f"--predictions_json={output_path}",
            "--country=USA", "--admin1_region=TX", "--batch_size=8",
            "--bypass_prompts", "--ignore_existing_predictions",
        ]
        child_env = {
            key: value for key, value in os.environ.items()
            if key in SPECIESNET_ENV_ALLOWLIST
        }
        remaining = None if deadline is None else deadline - clock()
        if remaining is not None and remaining <= RECORDING_RESERVE_SECONDS:
            catalog.release_gate1_claim(claim_token)
            raise subprocess.TimeoutExpired(command, max(0.0, remaining))
        timeout = 900.0 if remaining is None else min(
            900.0, remaining - RECORDING_RESERVE_SECONDS
        )
        try:
            completed = subprocess.run(
                command, env=child_env, text=True, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            catalog.release_gate1_claim(claim_token)
            raise
        if completed.returncode:
            raise RuntimeError(f"SpeciesNet failed with exit code {completed.returncode}")
        model_output = json.loads(output_path.read_text())
        predictions = model_output.get("predictions")
        if not isinstance(predictions, list) or len(predictions) != len(pending):
            raise RuntimeError("SpeciesNet output did not match pending media")
        assessments = []
        seen_paths: set[str] = set()
        for prediction in predictions:
            path = str(Path(str(prediction.get("filepath"))).resolve())
            metadata = by_path.get(path)
            if metadata is None or path in seen_paths:
                raise RuntimeError("SpeciesNet returned an unknown or duplicate file")
            seen_paths.add(path)
            assessments.append(_assessment(prediction, metadata))
        if seen_paths != set(by_path):
            raise RuntimeError("SpeciesNet omitted one or more pending files")

    routed = route_events(assessments)
    payload = [
        {key: value for key, value in item.items() if key not in {"camera_id", "captured_at"}}
        for item in routed
    ]
    result = catalog.record_gate1_batch(MODEL_NAME, MODEL_VERSION, claim_token, payload)
    if not isinstance(result, Mapping) or not result.get("ok"):
        raise RuntimeError("Gate 1 batch was not recorded")
    expected_events = len({item["event_key"] for item in payload})
    if int(result.get("released") or 0) != expected_events:
        raise RuntimeError("Gate 1 claim was not atomically consumed")
    return {
        "ok": True,
        "pending": len(pending),
        "recorded": int(result.get("inserted") or 0),
        "review": sum(item["route"] == "review" for item in routed),
        "archived": sum(item["route"] == "archive" for item in routed),
        "event_duplicates": sum(item["route"] == "event_duplicate" for item in routed),
        "model": f"{MODEL_NAME} {MODEL_VERSION}",
    }


def run_catchup(
    environ: Mapping[str, str],
    *,
    limit: int = 50,
    time_budget_seconds: int = 720,
    reserve_seconds: int = 300,
    clock: Callable[[], float] = time.monotonic,
    run_batch: Callable[..., dict[str, Any]] = run_worker,
) -> dict[str, Any]:
    """Run complete bounded batches until empty or another batch may exceed the budget."""
    started = clock()
    deadline = started + time_budget_seconds
    totals = {
        "ok": True,
        "batches": 0,
        "claimed": 0,
        "recorded": 0,
        "review": 0,
        "archived": 0,
        "event_duplicates": 0,
        "model": f"{MODEL_NAME} {MODEL_VERSION}",
    }
    stop_reason = "queue_empty"
    while True:
        elapsed = clock() - started
        if totals["batches"] and time_budget_seconds - elapsed < reserve_seconds:
            stop_reason = "time_budget"
            break
        try:
            batch = run_batch(environ, limit=limit, deadline=deadline, clock=clock)
        except subprocess.TimeoutExpired:
            stop_reason = "time_budget"
            break
        if int(batch.get("pending") or 0) == 0:
            break
        totals["batches"] += 1
        totals["claimed"] += int(batch.get("pending") or 0)
        for field in ("recorded", "review", "archived", "event_duplicates"):
            totals[field] += int(batch.get(field) or 0)
    totals["stop_reason"] = stop_reason
    totals["elapsed_seconds"] = round(clock() - started, 3)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DeerID Gate 1 against pending cloud thumbnails")
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--until-empty", action="store_true")
    parser.add_argument("--time-budget-seconds", type=int, default=720)
    args = parser.parse_args()
    limit = max(1, min(args.limit, 50))
    if args.until_empty:
        result = run_catchup(
            os.environ,
            limit=limit,
            time_budget_seconds=max(60, min(args.time_budget_seconds, 720)),
        )
    else:
        result = run_worker(os.environ, limit=limit)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
