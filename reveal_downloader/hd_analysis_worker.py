"""Local returned-HD analysis worker for human profile review."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

from .catalog import SupabaseCatalog
from .gate1b_worker import DEFAULT_ENDPOINT, DEFAULT_MODEL, MODEL_DIGEST, ModelUnavailable

MODEL_NAME = "Ollama-Gemma4-Vision-HD"
MODEL_VERSION = "gemma4-e4b-c6eb396dbd59@hd-prompt-2026-08-11.1"
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 96 * 1024
PROMPT = (
    "Analyze this returned HD trail-camera image for human deer-profile review. "
    "Do not claim a precise age or antler score when pose, scale, visibility, or image quality is inadequate. "
    "List visible distinguishing features. Return only JSON matching the schema."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "species": {"type": "string", "enum": ["whitetail", "axis", "other_deer", "non_deer", "unknown"]},
        "sex": {"type": "string", "enum": ["male", "female", "unknown"]},
        "animal_count": {"type": "integer", "minimum": 0, "maximum": 20},
        "identity_eligible": {"type": "boolean"},
        "age_eligible": {"type": "boolean"},
        "age_class": {"type": "string", "enum": ["1_5", "2_5", "3_5", "mature_4_5_plus", "unknown"]},
        "antler_score_eligible": {"type": "boolean"},
        "antler_score_range": {"type": "string", "maxLength": 80},
        "distinguishing_features": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 12},
        "summary": {"type": "string", "maxLength": 500},
    },
    "required": ["species", "sex", "animal_count", "identity_eligible", "age_eligible", "age_class", "antler_score_eligible", "antler_score_range", "distinguishing_features", "summary"],
    "additionalProperties": False,
}


def normalize_result(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(SCHEMA["required"]):
        raise ModelUnavailable("HD model result fields are invalid")
    if value["species"] not in SCHEMA["properties"]["species"]["enum"] or value["sex"] not in SCHEMA["properties"]["sex"]["enum"]:
        raise ModelUnavailable("HD model classification is invalid")
    count = value["animal_count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 20:
        raise ModelUnavailable("HD animal count is invalid")
    for field in ("identity_eligible", "age_eligible", "antler_score_eligible"):
        if not isinstance(value[field], bool):
            raise ModelUnavailable("HD eligibility is invalid")
    if value["age_class"] not in SCHEMA["properties"]["age_class"]["enum"]:
        raise ModelUnavailable("HD age class is invalid")
    features = value["distinguishing_features"]
    if not isinstance(features, list) or len(features) > 12 or any(not isinstance(x, str) or not x.strip() or len(x) > 120 for x in features):
        raise ModelUnavailable("HD distinguishing features are invalid")
    for field, limit in (("antler_score_range", 80), ("summary", 500)):
        if not isinstance(value[field], str) or not value[field].strip() or len(value[field]) > limit:
            raise ModelUnavailable("HD narrative is invalid")
    if not value["age_eligible"] and value["age_class"] != "unknown":
        raise ModelUnavailable("ineligible age must remain unknown")
    if not value["antler_score_eligible"] and value["antler_score_range"] != "unknown":
        raise ModelUnavailable("ineligible antler score must remain unknown")
    return {**value, "distinguishing_features": [x.strip() for x in features], "summary": value["summary"].strip()}


class OllamaHDAnalyzer:
    def __init__(self, endpoint: str, model: str, deadline: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        if endpoint.rstrip("/") not in {"http://127.0.0.1:11434", "http://localhost:11434"} or model != DEFAULT_MODEL:
            raise ValueError("HD analyzer must use the pinned local model")
        self.endpoint = endpoint.rstrip("/"); self.model = model; self.deadline = deadline; self.clock = clock
        with urllib.request.urlopen(f"{self.endpoint}/api/tags", timeout=5) as response:
            tags = json.load(response)
        if not any(x.get("name") == DEFAULT_MODEL and x.get("digest") == MODEL_DIGEST for x in tags.get("models", [])):
            raise ModelUnavailable("local model digest is not pinned")

    def analyze(self, image: bytes) -> dict[str, Any]:
        if not image or len(image) > MAX_IMAGE_BYTES: raise ModelUnavailable("HD image is invalid")
        body = json.dumps({"model": self.model, "prompt": PROMPT, "images": [base64.b64encode(image).decode("ascii")], "stream": False, "format": SCHEMA, "options": {"temperature": 0}}, separators=(",", ":")).encode()
        request = urllib.request.Request(f"{self.endpoint}/api/generate", data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ModelUnavailable("local HD model unavailable") from exc
        if len(payload) > MAX_RESPONSE_BYTES: raise ModelUnavailable("HD model response is too large")
        try:
            outer = json.loads(payload); return normalize_result(json.loads(outer["response"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelUnavailable("HD model response is malformed") from exc


def run_worker(environ: Mapping[str, str], *, catalog_factory: Callable[..., Any] = SupabaseCatalog, analyzer_factory: Callable[..., Any] = OllamaHDAnalyzer, deadline: Optional[float] = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    if not environ.get("SUPABASE_URL") or not environ.get("SUPABASE_SECRET_KEY"): raise ValueError("Supabase configuration is required")
    catalog = catalog_factory(environ["SUPABASE_URL"], environ["SUPABASE_SECRET_KEY"], environ.get("SUPABASE_BUCKET", "tactacam-photos"))
    catalog.set_deadline(deadline or clock() + 900, clock=clock)
    claim = catalog.claim_hd_review(MODEL_NAME, MODEL_VERSION)
    if not isinstance(claim, Mapping) or not claim.get("ok"): raise RuntimeError("HD review claim failed")
    if claim.get("empty"): return {"ok": True, "empty": True, "completed": 0, "failed": 0}
    token = claim.get("claim_token")
    try:
        image = catalog.read_private_image(claim["object_path"], max_bytes=MAX_IMAGE_BYTES)
        analyzer = analyzer_factory(environ.get("HD_OLLAMA_URL", DEFAULT_ENDPOINT), DEFAULT_MODEL, deadline, clock)
        result = analyzer.analyze(image)
        completed = catalog.complete_hd_review(token, MODEL_NAME, MODEL_VERSION, result)
        if not isinstance(completed, Mapping) or not completed.get("ok"): raise RuntimeError("HD result recording failed")
    except ModelUnavailable:
        catalog.fail_hd_review(token, "model_unavailable")
        return {"ok": False, "empty": False, "completed": 0, "failed": 1}
    return {"ok": True, "empty": False, "completed": 1, "failed": 0}


def main() -> int:
    print(json.dumps(run_worker(os.environ), sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
