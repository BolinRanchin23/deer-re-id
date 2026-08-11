"""Local, fail-safe Gate 1B vision worker for DeerID event representatives."""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Optional

from .catalog import SupabaseCatalog
from .gate1b import normalize_prediction, triage_prediction

MODEL_NAME = "Ollama-Gemma4-Vision"
MODEL_VERSION = "gemma4-e4b-c6eb396dbd59@prompt-2026-08-11.1"
DEFAULT_MODEL = "gemma4:e4b"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
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


class OllamaVisionClient:
    def __init__(
        self,
        endpoint: str,
        model: str,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        *,
        transport: Optional[Any] = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Ollama endpoint must be local loopback HTTP")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Ollama endpoint is invalid")
        if not model or len(model) > 120:
            raise ValueError("Ollama model is invalid")
        self.endpoint = endpoint.rstrip("/")
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
            "prompt": PROMPT,
            "images": [base64.b64encode(image).decode("ascii")],
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0},
        }
        status, body = self.transport.request(
            "POST",
            f"{self.endpoint}/api/generate",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            timeout=timeout,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        if status != 200:
            raise ModelUnavailable("local model request failed")
        try:
            outer = json.loads(body.decode("utf-8"))
            if outer.get("done") is not True or not isinstance(
                outer.get("response"), str
            ):
                raise ValueError
            return normalize_prediction(json.loads(outer["response"]))
        except (
            AttributeError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ModelUnavailable("local model response is malformed") from exc


def run_worker(
    environ: Mapping[str, str],
    *,
    limit: int = 20,
    catalog_factory: Callable[..., Any] = SupabaseCatalog,
    analyzer_factory: Callable[..., Any] = OllamaVisionClient,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not secret:
        raise ValueError("Supabase configuration is required")
    bounded_limit = max(1, min(int(limit), 60))
    catalog = catalog_factory(
        url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
    )
    catalog.set_deadline(deadline or (clock() + 1800), clock=clock)
    pending = catalog.read_gate1b_pending(MODEL_NAME, MODEL_VERSION, bounded_limit)
    if not isinstance(pending, list) or len(pending) > bounded_limit:
        raise RuntimeError("Gate 1B pending response is invalid")
    analyzer = analyzer_factory(
        environ.get("GATE1B_OLLAMA_URL", DEFAULT_ENDPOINT),
        environ.get("GATE1B_OLLAMA_MODEL", DEFAULT_MODEL),
        deadline,
        clock,
    )
    rows: list[dict[str, Any]] = []
    counts = {"likely_male": 0, "uncertain": 0, "female_candidate": 0}
    failed_count = 0
    for item in pending:
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
    recorded = 0
    if rows:
        result = catalog.record_gate1b_batch(MODEL_NAME, MODEL_VERSION, rows)
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise RuntimeError("Gate 1B predictions were not recorded")
        recorded = int(result.get("inserted", 0))
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
