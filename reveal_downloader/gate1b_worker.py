"""Local, fail-safe Gate 1B vision worker for DeerID event representatives."""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

from .catalog import GATE1B_MODEL_NAME as MODEL_NAME
from .catalog import GATE1B_MODEL_VERSION as MODEL_VERSION
from .catalog import (
    SupabaseCatalog,
)
from .gate1b import normalize_prediction, triage_prediction

DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
PROMPT = (
    "Inspect this trail-camera image conservatively. Count every visible animal first. "
    "mixed_group MUST be true whenever animal_count > 1. all_animals_assessed MUST be false "
    "if any visible animal is too distant, occluded, cropped, or blurry to classify. species "
    "is the best event-level deer species, or unknown for mixed or ambiguous species. "
    "visible_antler=yes or probable_male=yes means at least one animal has positive evidence. "
    "A lack of visible antlers never proves female: use unknown when any head is not fully "
    "assessable. Axis deer have a spotted coat and may also be called chital. Return only JSON."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "animal_count": {"type": "integer", "minimum": 0, "maximum": 20},
        "species": {
            "type": "string",
            "enum": ["whitetail", "axis", "other_deer", "non_deer", "unknown"],
        },
        "visible_antler": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "probable_male": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "head_visibility": {
            "type": "string",
            "enum": ["full", "partial", "none", "unknown"],
        },
        "lighting": {"type": "string", "enum": ["day_color", "night_ir", "unknown"]},
        "mixed_group": {"type": "boolean"},
        "all_animals_assessed": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 300},
    },
    "required": [
        "animal_count",
        "species",
        "visible_antler",
        "probable_male",
        "head_visibility",
        "lighting",
        "mixed_group",
        "all_animals_assessed",
        "reason",
    ],
    "additionalProperties": False,
}


class ModelUnavailable(RuntimeError):
    """The local model did not return a trustworthy structured result."""


def handle_cron(
    environ: Mapping[str, str],
    authorization: Optional[str],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, dict[str, Any]]:
    secret = environ.get("CRON_SECRET", "")
    if len(secret) < 16:
        return 503, {"ok": False, "error": "Gate 1B cron is not configured"}
    supplied = authorization or ""
    if not hmac.compare_digest(supplied, "Bearer " + secret):
        return 401, {"ok": False, "error": "unauthorized"}
    try:
        result = run_worker(environ, limit=10, deadline=clock() + 240.0, clock=clock)
    except (OSError, RuntimeError, TypeError, ValueError):
        return 503, {"ok": False, "error": "Gate 1B worker unavailable"}
    failed = int(result.get("failed", 0))
    if failed:
        return 503, {"ok": False, "error": "Gate 1B batch incomplete", "failed": failed}
    return 200, result


class _HTTPTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(max_response_bytes + 1)
                if len(payload) > max_response_bytes:
                    raise ModelUnavailable("model response is too large")
                return response.status, payload
        except (OSError, urllib.error.URLError) as exc:
            raise ModelUnavailable("local model is unavailable") from exc


class OpenAIVisionClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        *,
        transport: Optional[Any] = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("OpenAI API key is required")
        if model != DEFAULT_MODEL:
            raise ValueError("OpenAI model must be the pinned snapshot")
        self.api_key = api_key.strip()
        self.model = model
        self.deadline = deadline
        self.clock = clock
        self.transport = transport or _HTTPTransport()


    def analyze(self, image: bytes) -> dict[str, Any]:
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ModelUnavailable("image is invalid")
        timeout = 180.0
        if self.deadline is not None:
            timeout = min(timeout, max(1.0, self.deadline - self.clock()))
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"), "detail": "low"}},
            ]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "deerid_gate1b", "strict": True, "schema": SCHEMA}},
            "temperature": 0,
            "max_tokens": 700,
        }
        status, body = self.transport.request(
            "POST",
            OPENAI_ENDPOINT,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            timeout=timeout,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        if status != 200:
            raise ModelUnavailable("OpenAI model request failed")
        try:
            outer = json.loads(body.decode("utf-8"))
            content = outer["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
            return normalize_prediction(json.loads(content))
        except (
            AttributeError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ModelUnavailable("OpenAI model response is malformed") from exc


def run_worker(
    environ: Mapping[str, str],
    *,
    limit: int = 20,
    catalog_factory: Callable[..., Any] = SupabaseCatalog,
    analyzer_factory: Callable[..., Any] = OpenAIVisionClient,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not secret:
        raise ValueError("Supabase configuration is required")
    api_key = environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OpenAI configuration is required")
    bounded_limit = max(1, min(int(limit), 60))
    catalog = catalog_factory(
        url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
    )
    catalog.set_deadline(deadline or (clock() + 1800), clock=clock)
    claim = catalog.claim_gate1b_batch(MODEL_NAME, MODEL_VERSION, bounded_limit)
    if not isinstance(claim, Mapping) or not claim.get("ok"):
        raise RuntimeError("Gate 1B claim response is invalid")
    if claim.get("empty"):
        return {"ok":True,"pending":0,"recorded":0,"failed":0,"likely_male":0,"uncertain":0,"female_candidate":0}
    pending=claim.get("items");claim_token=claim.get("claim_token")
    if not isinstance(pending, list) or len(pending) > bounded_limit or not isinstance(claim_token,str):
        raise RuntimeError("Gate 1B claim response is invalid")
    analyzer = analyzer_factory(
        api_key,
        DEFAULT_MODEL,
        deadline,
        clock,
    )
    rows: list[dict[str, Any]] = []
    counts = {"likely_male": 0, "uncertain": 0, "female_candidate": 0}
    failed_count = 0
    for offset,item in enumerate(pending):
        if deadline is not None and clock() >= deadline - 30.0:
            failed_count += len(pending) - offset
            break
        try:
            image = catalog.read_private_image(
                item["object_path"], max_bytes=MAX_IMAGE_BYTES
            )
            prediction = analyzer.analyze(image)
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            ModelUnavailable,
        ):
            # Do not turn an unavailable model into durable evidence. The item remains
            # pending for the same version and the ordinary review route stays uncertain.
            failed_count += 1
            continue
        triage = triage_prediction(prediction)
        counts[triage] += 1
        rows.append(
            {
                "media_id": item["media_id"],
                "gate1_assessment_id": item["gate1_assessment_id"],
                "event_key": item["event_key"],
                "species_label": prediction["species"],
                "visible_antler": prediction["visible_antler"],
                "probable_male": prediction["probable_male"],
                "head_visibility": prediction["head_visibility"],
                "lighting": prediction["lighting"],
                "animal_count": prediction["animal_count"],
                "mixed_group": prediction["mixed_group"],
                "all_animals_assessed": prediction["all_animals_assessed"],
                "triage_class": triage,
                "hd_recommended": triage == "likely_male",
                "model_failure": False,
                "reason": prediction["reason"],
                "raw_output": prediction,
            }
        )
    result = catalog.complete_gate1b_batch(claim_token, MODEL_NAME, MODEL_VERSION, rows)
    if not isinstance(result, Mapping) or not result.get("ok"):
        raise RuntimeError("Gate 1B predictions were not recorded")
    recorded=int(result.get("persisted",-1));unfinished=int(result.get("unfinished",-1))
    if recorded != len(rows) or unfinished != failed_count:
        raise RuntimeError("Gate 1B durable completion is incomplete")
    return {
        "ok": True,
        "pending": len(pending),
        "recorded": recorded,
        "failed": failed_count,
        **counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(run_worker(os.environ, limit=args.limit), separators=(",", ":")))


if __name__ == "__main__":
    main()
