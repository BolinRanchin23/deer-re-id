"""Authenticated private photo library and camera-map helpers."""

import hashlib
import hmac
import logging
import json
import math
import re
import time
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .archive import detected_image_extension
from .client import HDRequestRejected, RevealClient, RevealError
from .supabase import (
    MAX_STORAGE_JSON_BYTES,
    StorageError,
    StorageTransport,
    SupabaseArchive,
    _postgrest_auth_headers,
    _project_origin,
)

LIBRARY_DEADLINE_SECONDS = 25.0
INTERACTIVE_DEADLINE_SECONDS = 8.0
LIBRARY_PREVIEW_SECONDS = 300
LIBRARY_REVIEW_SECONDS = 900
HD_REQUEST_DEADLINE_SECONDS = 15.0
MAX_LIBRARY_PHOTOS = 10
MAX_LIBRARY_PREVIEW_BYTES = 8 * 1024 * 1024
GATE1_MODEL_NAME = "SpeciesNet"
GATE1_MODEL_VERSION = "4.0.3a"
GATE1B_MODEL_NAME = "OpenAI-GPT-4o-mini-Vision"
GATE1B_MODEL_VERSION = "gpt-4o-mini-2024-07-18@prompt-2026-08-12.1"
_UUID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_OBJECT_PATH = re.compile(
    r"\A[A-Za-z0-9-][A-Za-z0-9._-]{0,63}@[0-9a-f]{64}/"
    r"\d{4}/\d{2}/\d{2}/(?:"
    r"\d{8}T\d{6}Z_[A-Za-z0-9-][A-Za-z0-9._-]{0,63}@[0-9a-f]{64}\.(?:jpg|png)"
    r"|\d{8}T\d{6}Z_[A-Za-z0-9][A-Za-z0-9.-]{0,79}@[0-9a-f]{64}_hd\.(?:jpg|png)"
    r")\Z"
)
_MAPBOX_TOKEN = re.compile(r"\Apk\.[A-Za-z0-9._-]{8,500}\Z")


class SupabaseCatalog:
    """Minimal service-role PostgREST client for private DeerID RPCs."""

    def __init__(
        self,
        base_url: str,
        secret_key: str,
        bucket: str = "tactacam-photos",
        *,
        transport: Optional[Any] = None,
    ) -> None:
        if not secret_key:
            raise ValueError("Supabase secret key is required")
        self.base_url = _project_origin(base_url)
        self._transport = transport or StorageTransport()
        self._headers = {
            **_postgrest_auth_headers(secret_key),
            "Content-Type": "application/json",
        }
        self._archive = SupabaseArchive(
            self.base_url, secret_key, bucket, transport=self._transport
        )

    def set_deadline(self, deadline: float, *, clock: Callable[[], float]) -> None:
        self._archive.set_deadline(deadline, clock=clock)

    def _rpc(self, name: str, payload: Dict[str, Any]) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self._transport.request(
            "POST",
            f"{self.base_url}/rest/v1/rpc/{name}",
            headers=self._headers,
            body=body,
            max_response_bytes=MAX_STORAGE_JSON_BYTES,
        )
        if response.status != 200:
            detail = ""
            try:
                error_payload = json.loads(response.body.decode("utf-8"))
                message = error_payload.get("message") if isinstance(error_payload, Mapping) else None
                code = error_payload.get("code") if isinstance(error_payload, Mapping) else None
                if isinstance(message, str) and message and len(message) <= 200 and isinstance(code, str) and len(code) <= 40:
                    detail = f" [{code}] {message}"
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise StorageError(
                f"Supabase private catalog RPC failed with HTTP {response.status}{detail}",
                http_status=response.status,
            )
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError("Private catalog is unavailable") from exc

    def read_library(self, limit: int = MAX_LIBRARY_PHOTOS) -> Any:
        return self._rpc(
            "deerid_private_library", {"p_limit": max(1, min(60, int(limit)))}
        )

    def read_profiles(self) -> Any:
        return self._rpc("deerid_profiles", {})

    def read_profile_gallery_page(self, profile_id: str, limit: int = 24) -> Any:
        return self._rpc(
            "deerid_profile_gallery_page",
            {"p_profile_id": profile_id, "p_limit": max(1, min(60, int(limit)))},
        )

    def read_camera_map(self) -> Any:
        return self._rpc("deerid_private_camera_map", {})

    def read_gate1_funnel(self, model_name: str, model_version: str) -> Any:
        return self._rpc(
            "deerid_gate1_funnel",
            {"p_model_name": model_name, "p_model_version": model_version},
        )

    def read_gate1b_metrics(self) -> Any:
        return self._rpc("deerid_gate1b_metrics", {})

    def read_operational_stats(self) -> Any:
        return self._rpc("deerid_operational_stats", {})

    def read_process_overview(self) -> Any:
        return self._rpc("deerid_process_overview", {})

    def read_pipeline_health(self) -> Any:
        return self._rpc("deerid_pipeline_health", {})

    def query_all_photos(self, filters: Mapping[str, Any]) -> Any:
        return self._rpc("deerid_all_photos", {"p_" + key: value for key, value in filters.items()})

    def set_profile_representative(self, assignment_event_id: int, profile_id: str) -> Any:
        return self._rpc("deerid_set_profile_representative", {"p_assignment_event_id": assignment_event_id, "p_profile_id": profile_id})

    def read_gate1b_pending(
        self, model_name: str, model_version: str, limit: int = 20
    ) -> Any:
        return self._rpc(
            "deerid_gate1b_pending",
            {
                "p_model_name": model_name,
                "p_model_version": model_version,
                "p_limit": max(1, min(60, int(limit))),
            },
        )

    def claim_gate1b_batch(
        self, model_name: str, model_version: str, limit: int = 10
    ) -> Any:
        return self._rpc(
            "deerid_claim_gate1b_batch",
            {"p_model_name":model_name,"p_model_version":model_version,"p_limit":max(1,min(20,int(limit)))},
        )

    def complete_gate1b_batch(
        self, claim_token: str, model_name: str, model_version: str, results: list[dict[str, Any]]
    ) -> Any:
        return self._rpc(
            "deerid_complete_gate1b_batch",
            {"p_claim_token":claim_token,"p_model_name":model_name,"p_model_version":model_version,"p_results":results},
        )

    def record_gate1b_batch(
        self, model_name: str, model_version: str, results: list[dict[str, Any]]
    ) -> Any:
        return self._rpc(
            "deerid_record_gate1b_batch",
            {
                "p_model_name": model_name,
                "p_model_version": model_version,
                "p_results": results,
            },
        )

    def record_gate1b_label(
        self,
        media_id: str,
        assessment_id: int,
        review_version: int,
        species_label: str,
        visible_antler: str,
        probable_male: str,
        head_visibility: str,
        note: str,
    ) -> Any:
        return self._rpc(
            "deerid_record_gate1b_label",
            {
                "p_media_id": media_id,
                "p_assessment_id": assessment_id,
                "p_review_version": review_version,
                "p_species_label": species_label,
                "p_visible_antler": visible_antler,
                "p_probable_male": probable_male,
                "p_head_visibility": head_visibility,
                "p_note": note or None,
            },
        )

    def create_profile_from_review(
        self,
        media_id: str,
        assessment_id: int,
        review_version: int,
        display_name: str,
        species: str,
        sex: str,
        notes: str,
    ) -> Any:
        return self._rpc(
            "deerid_create_profile_from_review",
            {
                "p_media_id": media_id,
                "p_assessment_id": assessment_id,
                "p_review_version": review_version,
                "p_display_name": display_name,
                "p_species": species,
                "p_sex": sex,
                "p_notes": notes or None,
            },
        )

    def attach_media_to_profile(
        self,
        media_id: str,
        assessment_id: int,
        review_version: int,
        profile_id: str,
    ) -> Any:
        return self._rpc(
            "deerid_attach_media_to_profile_from_review",
            {
                "p_media_id": media_id,
                "p_assessment_id": assessment_id,
                "p_review_version": review_version,
                "p_profile_id": profile_id,
            },
        )

    def resolve_media_object(self, media_id: str) -> Any:
        return self._rpc("deerid_private_media_object", {"p_media_id": media_id})

    def record_review(
        self,
        media_id: str,
        assessment_id: int,
        review_version: int,
        action: str,
        note: str,
    ) -> Any:
        return self._rpc(
            "deerid_record_review_decision",
            {
                "p_media_id": media_id,
                "p_assessment_id": assessment_id,
                "p_review_version": review_version,
                "p_action": action,
                "p_note": note or None,
            },
        )

    def begin_hd_request(
        self, media_id: str, assessment_id: int, review_version: int, note: str
    ) -> Any:
        return self._rpc(
            "deerid_begin_hd_request",
            {
                "p_media_id": media_id,
                "p_assessment_id": assessment_id,
                "p_review_version": review_version,
                "p_note": note or None,
            },
        )

    def complete_hd_request(self, request_token: str) -> Any:
        return self._rpc(
            "deerid_complete_hd_request", {"p_request_token": request_token}
        )

    def fail_hd_request(self, request_token: str, error_code: str) -> Any:
        return self._rpc(
            "deerid_fail_hd_request",
            {"p_request_token": request_token, "p_error_code": error_code},
        )

    def mark_hd_request_unknown(self, request_token: str, error_code: str) -> Any:
        return self._rpc(
            "deerid_mark_hd_request_unknown",
            {"p_request_token": request_token, "p_error_code": error_code},
        )

    def claim_queued_hd_request(self) -> Any:
        return self._rpc("deerid_claim_queued_hd_request", {})

    def claim_hd_review(self, model_name: str, model_version: str, media_asset_id: Optional[str] = None) -> Any:
        return self._rpc("deerid_claim_hd_review", {"p_model_name": model_name, "p_model_version": model_version, "p_media_asset_id": media_asset_id})

    def complete_hd_review(self, claim_token: str, model_name: str, model_version: str, result: Mapping[str, Any]) -> Any:
        return self._rpc("deerid_complete_hd_review", {"p_claim_token": claim_token, "p_model_name": model_name, "p_model_version": model_version, "p_result": dict(result)})

    def fail_hd_review(self, claim_token: str, error_category: str) -> Any:
        return self._rpc("deerid_fail_hd_review", {"p_claim_token": claim_token, "p_error_category": error_category})

    def resolve_media_asset_object(self, media_asset_id: str) -> Any:
        return self._rpc("deerid_resolve_media_asset_object", {"p_media_asset_id": media_asset_id})

    def read_automation_audit(self, limit: int = 120) -> Any:
        return self._rpc("deerid_gate1b_automation_audit", {"p_limit": limit})

    def read_hd_review_queue_page(
        self, limit: int, camera_id: Optional[str] = None, queue: str = "active"
    ) -> Any:
        return self._rpc(
            "deerid_hd_review_queue_page",
            {
                "p_limit": max(1, min(31, int(limit))),
                "p_camera_id": camera_id,
                "p_queue": queue,
            },
        )

    def read_hd_review_progress(self) -> Any:
        return self._rpc("deerid_hd_review_progress", {})

    def record_hd_review_workflow_action(
        self,
        result_id: int,
        instance_id: str,
        action: str,
        reason: Optional[str] = None,
        note: str = "",
    ) -> Any:
        return self._rpc(
            "deerid_record_hd_review_workflow_action",
            {
                "p_hd_review_result_id": result_id,
                "p_hd_animal_instance_id": instance_id,
                "p_action": action,
                "p_reason": reason,
                "p_note": note or None,
            },
        )

    def record_automation_label(self, event_id: int, verdict: str, note: str = "") -> Any:
        return self._rpc("deerid_record_gate1b_automation_label", {"p_automation_event_id": event_id, "p_verdict": verdict, "p_note": note or None})

    def record_hd_review_decision(self, result_id: int, action: str, *, instance_id: Optional[str] = None, profile_id: Optional[str] = None, display_name: str = "", species: str = "", sex: str = "", note: str = "") -> Any:
        return self._rpc("deerid_record_hd_review_decision", {"p_hd_review_result_id": result_id, "p_action": action, "p_profile_id": profile_id, "p_display_name": display_name or None, "p_species": species or None, "p_sex": sex or None, "p_note": note or None, "p_hd_animal_instance_id": instance_id})

    def propose_hd_profile_assignment(self, result_id: int, instance_id: str, action: str, **fields: Any) -> Any:
        return self._rpc("deerid_propose_hd_profile_assignment", {"p_hd_review_result_id": result_id, "p_hd_animal_instance_id": instance_id, "p_action": action, "p_profile_id": fields.get("profile_id"), "p_display_name": fields.get("display_name") or None, "p_species": fields.get("species") or None, "p_sex": fields.get("sex") or None, "p_note": fields.get("note") or None})

    def review_hd_profile_assignment(self, proposal_id: str, event_id: int, action: str) -> Any:
        rpc = "deerid_confirm_hd_profile_assignment" if action == "confirm" else "deerid_undo_hd_profile_assignment"
        return self._rpc(rpc, {"p_proposal_id": proposal_id, "p_expected_event_id": event_id})

    def correct_hd_instance_bbox(self, result_id: int, instance_id: str, expected_event_id: Optional[int], bbox: Mapping[str, Any], reason: str, note: str = "") -> Any:
        return self._rpc("deerid_correct_hd_instance_bbox", {"p_hd_review_result_id": result_id, "p_hd_animal_instance_id": instance_id, "p_expected_geometry_event_id": expected_event_id, "p_bbox_x": bbox["x"], "p_bbox_y": bbox["y"], "p_bbox_width": bbox["width"], "p_bbox_height": bbox["height"], "p_reason": reason, "p_note": note or None})

    def correct_hd_instance_topology(self, result_id: int, instance_id: str, expected_event_id: Optional[int], request_id: str, action: str, boxes: list[Mapping[str, Any]], note: str = "") -> Any:
        return self._rpc("deerid_correct_hd_instance_topology", {"p_hd_review_result_id": result_id, "p_source_instance_id": instance_id, "p_expected_topology_event_id": expected_event_id, "p_request_id": request_id, "p_action": action, "p_boxes": boxes, "p_note": note or None})

    def reassign_hd_instance(self, assignment_event_id: int, profile_id: str) -> Any:
        return self._rpc("deerid_reassign_hd_instance", {"p_assignment_event_id": assignment_event_id, "p_profile_id": profile_id})

    def read_gate1_pending(
        self, model_name: str, model_version: str, limit: int = 60
    ) -> Any:
        return self._rpc(
            "deerid_gate1_pending",
            {
                "p_model_name": model_name,
                "p_model_version": model_version,
                "p_limit": limit,
            },
        )

    def record_gate1_batch(
        self,
        model_name: str,
        model_version: str,
        claim_token: str,
        results: list[dict[str, Any]],
    ) -> Any:
        return self._rpc(
            "deerid_record_gate1_batch",
            {
                "p_model_name": model_name,
                "p_model_version": model_version,
                "p_claim_token": claim_token,
                "p_results": results,
            },
        )

    def release_gate1_claim(self, claim_token: str) -> Any:
        return self._rpc("deerid_release_gate1_claim", {"p_claim_token": claim_token})

    def read_private_image(self, object_path: str, *, max_bytes: int) -> bytes:
        return self._archive.read_private_image(object_path, max_bytes=max_bytes)


def handle_library(
    environ: Mapping[str, str],
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    now: Optional[float] = None,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Return the open-prototype catalog through the server-side service key."""
    signing_key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    url = environ.get("SUPABASE_URL", "")
    key = environ.get("SUPABASE_SECRET_KEY", "")
    if signing_key is None or not url or not key:
        return 404, {"ok": False, "error": "not found"}
    clock = (lambda: now) if now is not None else time.monotonic
    try:
        catalog = catalog_factory(
            url, key, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        catalog.set_deadline(clock() + LIBRARY_DEADLINE_SECONDS, clock=clock)
        # All Photos owns the paginated archive feed in production; loading it
        # here duplicates a large nested RPC response and can make every dashboard
        # view fail. Test/injected catalogs retain the legacy seam used by action tests.
        photos = (
            []
            if catalog_factory is SupabaseCatalog
            else _sanitize_photos(
                catalog.read_library(MAX_LIBRARY_PHOTOS), signing_key, current
            )
        )
        cameras = _sanitize_cameras(catalog.read_camera_map())
        profiles = _sanitize_profiles(catalog.read_profiles(), signing_key, current)
        pipeline = _sanitize_pipeline(
            catalog.read_gate1_funnel(GATE1_MODEL_NAME, GATE1_MODEL_VERSION)
        )
        gate1b = _sanitize_gate1b_metrics(catalog.read_gate1b_metrics())
        stats = _sanitize_operational_stats(catalog.read_operational_stats())
        process_overview = _sanitize_process_overview(catalog.read_process_overview())
        pipeline_health = _sanitize_pipeline_health(catalog.read_pipeline_health())
        automation_audit = _sanitize_auxiliary_media_rows(catalog.read_automation_audit(20), signing_key, current, "automation_event_id")
        raw_hd_progress = catalog.read_hd_review_progress()
        hd_review_progress = {
            "total": max(0, int(raw_hd_progress.get("total", 0))),
            "completed": max(0, int(raw_hd_progress.get("completed", 0))),
            "remaining": max(0, int(raw_hd_progress.get("remaining", 0))),
            "profiling_ready": max(0, int(raw_hd_progress.get("profiling_ready", 0))),
            "deferred": max(0, int(raw_hd_progress.get("deferred", 0))),
            "pending_confirmation": max(0, int(raw_hd_progress.get("pending_confirmation", 0))),
            "detector_errors": max(0, int(raw_hd_progress.get("detector_errors", 0))),
            "removed_detections": max(0, int(raw_hd_progress.get("removed_detections", 0))),
            "inseparable": max(0, int(raw_hd_progress.get("inseparable", 0))),
            "by_camera": {
                camera_id: max(0, int(count))
                for camera_id, count in dict(raw_hd_progress.get("by_camera") or {}).items()
                if isinstance(camera_id, str) and _UUID.fullmatch(camera_id)
            },
        } if isinstance(raw_hd_progress, Mapping) else {"total": 0, "completed": 0, "remaining": 0}
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        logging.exception("DeerID library assembly failed")
        return 503, {"ok": False, "error": "library unavailable"}
    payload: Dict[str, Any] = {
        "ok": True,
        "photos": photos,
        "cameras": cameras,
        "profiles": profiles,
        "pipeline": pipeline,
        "gate1b": gate1b,
        "stats": stats,
        "process_overview": process_overview,
        "pipeline_health": pipeline_health,
        "automation_audit": automation_audit,
        "hd_review_queue": [],
        "hd_review_progress": hd_review_progress,
    }
    mapbox_token = environ.get("NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN", "").strip()
    if _MAPBOX_TOKEN.fullmatch(mapbox_token):
        payload["mapbox_access_token"] = mapbox_token
    return 200, payload


def handle_profile_gallery(
    environ: Mapping[str, str],
    query: Mapping[str, Any],
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Return one bounded profile gallery without coupling it to app bootstrap."""
    signing_key = _signing_key(environ)
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    profile_id = query.get("profile_id")
    if (
        signing_key is None
        or not url
        or not secret
        or not isinstance(profile_id, str)
        or _UUID.fullmatch(profile_id) is None
    ):
        return 400, {"ok": False, "error": "invalid profile"}
    try:
        limit = int(query.get("limit", 24))
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "invalid profile"}
    if limit < 1 or limit > 60:
        return 400, {"ok": False, "error": "invalid profile"}
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        rows = _sanitize_profile_gallery(
            catalog.read_profile_gallery_page(profile_id, limit),
            signing_key,
            current,
            expected_profile_id=profile_id,
            limit=limit,
        )
        if any(row.get("profile_id") != profile_id for row in rows):
            raise StorageError("Profile gallery is unavailable")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        logging.exception("DeerID profile gallery assembly failed")
        return 503, {"ok": False, "error": "profile gallery unavailable"}
    return 200, {"ok": True, "items": rows}


def handle_hd_review_queue(
    environ: Mapping[str, str],
    query: Mapping[str, Any],
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Return one bounded authoritative profiling-queue refill page."""
    signing_key = _signing_key(environ)
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    try:
        limit = int(query.get("limit", 15))
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "invalid queue request"}
    camera_id = query.get("camera_id") or None
    queue = query.get("queue", "active")
    if (
        signing_key is None
        or not url
        or not secret
        or limit < 1
        or limit > 30
        or (camera_id is not None and (not isinstance(camera_id, str) or _UUID.fullmatch(camera_id) is None))
        or not isinstance(queue, str)
        or queue not in {"active", "deferred", "pending", "issues"}
    ):
        return 400, {"ok": False, "error": "invalid queue request"}
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        clock = time.monotonic
        catalog.set_deadline(clock() + INTERACTIVE_DEADLINE_SECONDS, clock=clock)
        raw = catalog.read_hd_review_queue_page(limit + 1, camera_id, queue)
        if not isinstance(raw, Mapping) or not isinstance(raw.get("items"), list):
            raise StorageError("HD review queue is unavailable")
        raw_items = raw["items"]
        items = _sanitize_hd_review_queue_page(raw_items[:limit], signing_key, current, limit)
        if not isinstance(raw.get("has_more"), bool): raise StorageError("HD review queue is unavailable")
        has_more = raw["has_more"] or len(raw_items) > limit
        raw_progress = raw.get("progress")
        if not isinstance(raw_progress, Mapping): raise StorageError("HD review queue is unavailable")
        progress: Dict[str, Any] = {field: max(0, int(raw_progress.get(field, 0))) for field in ("total","completed","remaining","profiling_ready","deferred","pending_confirmation","detector_errors","removed_detections","inseparable")}
        progress["by_camera"] = {camera: max(0,int(count)) for camera,count in dict(raw_progress.get("by_camera") or {}).items() if isinstance(camera,str) and _UUID.fullmatch(camera)}
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        logging.exception("DeerID HD review queue assembly failed")
        return 503, {"ok": False, "error": "HD review queue unavailable"}
    return 200, {"ok": True, "items": items, "has_more": has_more, "progress": progress}


def _mutation_error_response(exc: Exception, unavailable: str) -> Tuple[int, Dict[str, Any]]:
    message = str(exc).lower()
    if any(marker in message for marker in ("stale", "conflict", "already resolved", "already assigned", "not pending", "pending assignment", "terminal", "too many animal instances")):
        return 409, {"ok": False, "error": "conflicting workflow state"}
    if "invalid" in message or "not found" in message:
        return 400, {"ok": False, "error": "invalid workflow action"}
    return 503, {"ok": False, "error": unavailable}


def handle_hd_review_workflow(
    environ: Mapping[str, str],
    token: str,
    instance_id: str,
    action: str,
    *,
    reason: Optional[str] = None,
    note: str = "",
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Append one durable per-animal profiling workflow transition."""
    signing_key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = (
        _verify_hd_instance_action_token(token, signing_key, current)
        if signing_key is not None
        else None
    )
    result_id = capability[0] if capability is not None else None
    allowed_reasons = {
        "wrong_deer", "box_clipped", "multiple_deer", "missed_deer",
        "inseparable", "false_detection", "other",
    }
    if capability is None or not isinstance(instance_id, str) or capability[1] != instance_id:
        return 404, {"ok": False, "error": "not found"}
    if (
        not isinstance(action, str)
        or action not in {"defer", "reopen", "detector_error"}
        or (reason is not None and (not isinstance(reason, str) or reason not in allowed_reasons))
        or (action == "detector_error" and reason is None)
        or not isinstance(note, str)
        or len(note) > 500
    ):
        return 400, {"ok": False, "error": "invalid profiling workflow action"}
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not secret:
        return 404, {"ok": False, "error": "not found"}
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        clock = time.monotonic
        catalog.set_deadline(clock() + INTERACTIVE_DEADLINE_SECONDS, clock=clock)
        result = catalog.record_hd_review_workflow_action(
            result_id, instance_id, action, reason, note.strip()
        )
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise StorageError("Profiling workflow action failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError) as exc:
        logging.exception("DeerID profiling workflow action failed")
        return _mutation_error_response(exc, "profiling workflow unavailable")
    return 200, {field: result[field] for field in ("ok","state","event_id","replayed","hd_animal_instance_id") if field in result}


def handle_hd_profile_assignment_review(
    environ: Mapping[str, str], token: str, proposal_id: str, action: str, *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Confirm or undo one pending profile assignment proposal."""
    signing_key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    event_id = _verify_aux_action_token(token, "proposal", signing_key, current) if signing_key is not None else None
    if event_id is None:
        return 404, {"ok": False, "error": "not found"}
    if not isinstance(proposal_id, str) or _UUID.fullmatch(proposal_id) is None or not isinstance(action, str) or action not in {"confirm", "undo"}:
        return 400, {"ok": False, "error": "invalid pending assignment action"}
    url = environ.get("SUPABASE_URL", ""); secret = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not secret: return 404, {"ok": False, "error": "not found"}
    try:
        catalog = catalog_factory(url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos"))
        clock = time.monotonic; catalog.set_deadline(clock() + INTERACTIVE_DEADLINE_SECONDS, clock=clock)
        result = catalog.review_hd_profile_assignment(proposal_id, event_id, action)
        if not isinstance(result, Mapping) or not result.get("ok"): raise StorageError("Pending assignment action failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError) as exc:
        logging.exception("DeerID pending assignment action failed")
        return _mutation_error_response(exc, "pending assignment unavailable")
    return 200, {field: result[field] for field in ("ok","confirmed","undone","replayed","proposal_id","profile_id","assignment_event_id","hd_animal_instance_id") if field in result}


def handle_hd_geometry_correction(
    environ: Mapping[str, str], token: str, instance_id: str,
    expected_event_id: Optional[int], bbox: Mapping[str, Any], reason: str, *,
    note: str = "", catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Append one stale-safe corrected crop box without rewriting detector evidence."""
    signing_key = _signing_key(environ); current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = _verify_hd_instance_action_token(token, signing_key, current) if signing_key is not None else None
    result_id = capability[0] if capability is not None else None
    try:
        safe_bbox = _sanitize_bbox(bbox)
    except (AttributeError, TypeError, ValueError, StorageError):
        return 400, {"ok": False, "error": "invalid geometry correction"}
    if capability is None or not isinstance(instance_id, str) or capability[1] != instance_id:
        return 404, {"ok": False, "error": "not found"}
    if (expected_event_id is not None and (not isinstance(expected_event_id, int) or isinstance(expected_event_id, bool) or expected_event_id < 1)) or not isinstance(reason, str) or reason not in {"rebox", "clipped_antlers", "wrong_deer", "other"} or not isinstance(note, str) or len(note) > 500:
        return 400, {"ok": False, "error": "invalid geometry correction"}
    url = environ.get("SUPABASE_URL", ""); secret = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not secret: return 404, {"ok": False, "error": "not found"}
    try:
        catalog = catalog_factory(url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")); clock = time.monotonic; catalog.set_deadline(clock() + INTERACTIVE_DEADLINE_SECONDS, clock=clock)
        result = catalog.correct_hd_instance_bbox(result_id, instance_id, expected_event_id, safe_bbox, reason, note.strip())
        if not isinstance(result, Mapping) or not result.get("ok"): raise StorageError("Geometry correction failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError) as exc:
        logging.exception("DeerID geometry correction failed")
        return _mutation_error_response(exc, "geometry correction unavailable")
    return 200, {field: result[field] for field in ("ok","geometry_event_id","hd_animal_instance_id","bbox") if field in result}


def handle_hd_instance_topology(
    environ: Mapping[str, str], token: str, instance_id: str,
    expected_event_id: Optional[int], request_id: str, action: str, boxes: Any, *,
    note: str = "", catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Apply one idempotent append-only animal-instance topology correction."""
    signing_key = _signing_key(environ); current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = _verify_hd_instance_action_token(token, signing_key, current) if signing_key is not None else None
    if capability is None or not isinstance(instance_id, str) or capability[1] != instance_id:
        return 404, {"ok": False, "error": "not found"}
    if (
        not isinstance(request_id, str) or _UUID.fullmatch(request_id) is None
        or not isinstance(action, str) or action not in {"add", "split", "remove", "inseparable"}
        or (expected_event_id is not None and (not isinstance(expected_event_id, int) or isinstance(expected_event_id, bool) or expected_event_id < 1))
        or not isinstance(note, str) or len(note) > 500 or not isinstance(boxes, list)
    ):
        return 400, {"ok": False, "error": "invalid topology correction"}
    try:
        if uuid.UUID(request_id).version != 4:
            return 400, {"ok": False, "error": "invalid topology correction"}
    except ValueError:
        return 400, {"ok": False, "error": "invalid topology correction"}
    expected_count = 1 if action == "add" else None
    if (expected_count is not None and len(boxes) != expected_count) or (action == "split" and not 2 <= len(boxes) <= 5) or (action in {"remove", "inseparable"} and boxes):
        return 400, {"ok": False, "error": "invalid topology correction"}
    try:
        if any(not isinstance(box, Mapping) or set(box) != {"x", "y", "width", "height"} for box in boxes):
            raise StorageError("invalid topology box")
        safe_boxes = [_sanitize_bbox(box) for box in boxes]
        if any(box["x"] + box["width"] > 1 or box["y"] + box["height"] > 1 for box in safe_boxes):
            raise StorageError("invalid topology box")
    except (AttributeError, TypeError, ValueError, StorageError):
        return 400, {"ok": False, "error": "invalid topology correction"}
    url = environ.get("SUPABASE_URL", ""); secret = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not secret: return 404, {"ok": False, "error": "not found"}
    try:
        catalog = catalog_factory(url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")); clock = time.monotonic; catalog.set_deadline(clock() + INTERACTIVE_DEADLINE_SECONDS, clock=clock)
        result = catalog.correct_hd_instance_topology(capability[0], instance_id, expected_event_id, request_id, action, safe_boxes, note.strip())
        ids = result.get("resulting_instance_ids") if isinstance(result, Mapping) else None
        if not isinstance(result, Mapping) or result.get("ok") is not True or result.get("action") != action or result.get("source_instance_id") != instance_id or not isinstance(result.get("topology_event_id"), int) or isinstance(result.get("topology_event_id"), bool) or result["topology_event_id"] < 1 or not isinstance(ids, list) or len(ids) > 5 or len(set(ids)) != len(ids) or any(not isinstance(value, str) or _UUID.fullmatch(value) is None for value in ids) or (action == "add" and len(ids) != 1) or (action == "split" and not 2 <= len(ids) <= 5) or (action in {"remove", "inseparable"} and ids) or not isinstance(result.get("replayed"), bool): raise StorageError("Topology correction failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError) as exc:
        logging.exception("DeerID topology correction failed")
        return _mutation_error_response(exc, "topology correction unavailable")
    return 200, {field: result[field] for field in ("ok","action","topology_event_id","source_instance_id","resulting_instance_ids","replayed") if field in result}


def handle_library_preview(
    environ: Mapping[str, str],
    token: str,
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, str, bytes]:
    """Resolve a signed media UUID server-side and proxy one bounded image."""
    key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    media_id = _verify_media_token(token, key, current) if key is not None else None
    asset_id = _verify_asset_token(token, key, current) if key is not None else None
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if (media_id is None and asset_id is None) or not url or not secret:
        return _not_found()
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        clock = time.monotonic
        catalog.set_deadline(clock() + LIBRARY_DEADLINE_SECONDS, clock=clock)
        selected = catalog.resolve_media_asset_object(asset_id) if asset_id is not None else catalog.resolve_media_object(media_id)
        if isinstance(selected, list):
            selected = selected[0] if len(selected) == 1 else None
        object_path = (
            selected.get("object_path") if isinstance(selected, Mapping) else None
        )
        expected_type = (
            selected.get("content_type") if isinstance(selected, Mapping) else None
        )
        if (
            not isinstance(object_path, str)
            or _OBJECT_PATH.fullmatch(object_path) is None
        ):
            raise StorageError("Library preview is unavailable")
        body = catalog.read_private_image(
            object_path, max_bytes=MAX_LIBRARY_PREVIEW_BYTES
        )
        extension = detected_image_extension(body)
        content_type = "image/jpeg" if extension == ".jpg" else "image/png"
        if not object_path.endswith(extension) or expected_type != content_type:
            raise StorageError("Library preview is unavailable")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return _not_found()
    return 200, content_type, body


def handle_review(
    environ: Mapping[str, str],
    token: str,
    action: str,
    note: str = "",
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    reveal_factory: Callable[[str, str], Any] = RevealClient,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Record one bounded human decision and submit HD requests to Reveal."""
    if action not in {"request_hd", "keep_for_identity", "not_useful", "defer"}:
        return 400, {"ok": False, "error": "invalid action"}
    if not isinstance(note, str) or len(note) > 500:
        return 400, {"ok": False, "error": "invalid note"}
    key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = _verify_review_token(token, key, current) if key is not None else None
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if capability is None or not url or not secret:
        return 404, {"ok": False, "error": "not found"}
    media_id, assessment_id, review_version = capability
    if action == "request_hd" and (
        not environ.get("TACTACAM_USERNAME") or not environ.get("TACTACAM_PASSWORD")
    ):
        return 503, {"ok": False, "error": "HD request unavailable"}
    clock = time.monotonic
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        deadline = clock() + HD_REQUEST_DEADLINE_SECONDS
        catalog.set_deadline(deadline, clock=clock)
        if action == "request_hd":
            begun = catalog.begin_hd_request(
                media_id, assessment_id, review_version, note.strip()
            )
            if not isinstance(begun, Mapping) or not begun.get("ok"):
                raise StorageError("HD request could not begin")
            if not begun.get("should_request"):
                state = begun.get("status")
                if state in {"submitted", "available"}:
                    return 200, {
                        "ok": True,
                        "media_id": media_id,
                        "action": action,
                        "request_status": state,
                    }
                return 202, {
                    "ok": True,
                    "media_id": media_id,
                    "action": action,
                    "request_status": (
                        state if state in {"requesting", "unknown"} else "requesting"
                    ),
                }
            request_token = begun.get("request_token")
            provider_photo_id = begun.get("provider_photo_id")
            if (
                not isinstance(request_token, str)
                or _UUID.fullmatch(request_token) is None
                or not isinstance(provider_photo_id, str)
                or not provider_photo_id
            ):
                raise StorageError("HD request claim is malformed")
            client = reveal_factory(
                environ["TACTACAM_USERNAME"], environ["TACTACAM_PASSWORD"]
            )
            client.set_deadline(deadline, clock=clock)
            try:
                client.request_hd_photos([provider_photo_id])
            except HDRequestRejected:
                catalog.fail_hd_request(request_token, "provider_rejected")
                return 502, {"ok": False, "error": "HD request failed"}
            except (RevealError, OSError, RuntimeError, ValueError):
                catalog.mark_hd_request_unknown(
                    request_token, "provider_outcome_unknown"
                )
                return 202, {
                    "ok": True,
                    "media_id": media_id,
                    "action": action,
                    "request_status": "unknown",
                }
            result = catalog.complete_hd_request(request_token)
            if not isinstance(result, Mapping) or not result.get("ok"):
                raise StorageError("HD request finalization failed")
            return 200, {
                "ok": True,
                "media_id": media_id,
                "action": action,
                "request_status": "submitted",
            }
        result = catalog.record_review(
            media_id, assessment_id, review_version, action, note.strip()
        )
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise StorageError("Review decision failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return 503, {"ok": False, "error": "review unavailable"}
    return 200, {"ok": True, "media_id": media_id, "action": action}


def handle_profile_assignment(
    environ: Mapping[str, str],
    token: str,
    action: str,
    *,
    display_name: str = "",
    species: str = "",
    sex: str = "",
    profile_id: str = "",
    notes: str = "",
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Create a season profile from a review image or confirm it on an existing profile."""
    if (
        action not in {"create", "attach"}
        or not isinstance(notes, str)
        or len(notes) > 500
    ):
        return 400, {"ok": False, "error": "invalid profile assignment"}
    if action == "create" and (
        not isinstance(display_name, str)
        or not 1 <= len(display_name.strip()) <= 80
        or species not in {"white-tailed deer", "axis deer", "other deer"}
        or sex not in {"male", "female", "unknown"}
    ):
        return 400, {"ok": False, "error": "invalid profile assignment"}
    if action == "attach" and (
        not isinstance(profile_id, str) or _UUID.fullmatch(profile_id) is None
    ):
        return 400, {"ok": False, "error": "invalid profile assignment"}
    key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = _verify_review_token(token, key, current) if key is not None else None
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if capability is None or not url or not secret:
        return 404, {"ok": False, "error": "not found"}
    media_id, assessment_id, review_version = capability
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        clock = time.monotonic
        catalog.set_deadline(clock() + LIBRARY_DEADLINE_SECONDS, clock=clock)
        if action == "create":
            result = catalog.create_profile_from_review(
                media_id,
                assessment_id,
                review_version,
                display_name.strip(),
                species,
                sex,
                notes.strip(),
            )
        else:
            result = catalog.attach_media_to_profile(
                media_id, assessment_id, review_version, profile_id
            )
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise StorageError("Profile assignment failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return 503, {"ok": False, "error": "profile assignment unavailable"}
    return 200, dict(result)


def handle_hd_review_decision(
    environ: Mapping[str, str], token: str, action: str, *, instance_id: str = "", profile_id: str = "", display_name: str = "", species: str = "", sex: str = "", note: str = "", catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog, epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    key = _signing_key(environ); current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = _verify_hd_instance_action_token(token, key, current) if key is not None else None
    result_id = capability[0] if capability is not None else None
    if capability is None or not isinstance(instance_id, str) or capability[1] != instance_id or not isinstance(action, str) or action not in {"create_profile", "match_profile", "not_identity_worthy", "defer"} or any(not isinstance(x, str) for x in (profile_id, display_name, species, sex, note)) or len(note) > 500:
        return 404, {"ok": False, "error": "not found"}
    if action == "match_profile" and _UUID.fullmatch(profile_id) is None:
        return 400, {"ok": False, "error": "invalid HD review decision"}
    if action == "create_profile" and (not 1 <= len(display_name.strip()) <= 80 or species not in {"white-tailed deer", "axis deer", "other deer"} or sex not in {"male", "female", "unknown"}):
        return 400, {"ok": False, "error": "invalid HD review decision"}
    url=environ.get("SUPABASE_URL",""); secret=environ.get("SUPABASE_SECRET_KEY","")
    if not url or not secret: return 404, {"ok": False, "error": "not found"}
    try:
        catalog=catalog_factory(url,secret,environ.get("SUPABASE_BUCKET","tactacam-photos")); clock=time.monotonic; catalog.set_deadline(clock()+INTERACTIVE_DEADLINE_SECONDS,clock=clock)
        if action in {"create_profile", "match_profile"}:
            result=catalog.propose_hd_profile_assignment(result_id,instance_id,action,profile_id=profile_id or None,display_name=display_name,species=species,sex=sex,note=note.strip())
        else:
            result=catalog.record_hd_review_decision(result_id,action,instance_id=instance_id,profile_id=profile_id or None,display_name=display_name,species=species,sex=sex,note=note.strip())
        if not isinstance(result,Mapping) or not result.get("ok"): raise StorageError("HD review decision failed")
    except (AttributeError,OSError,RuntimeError,TypeError,ValueError,StorageError) as exc: return _mutation_error_response(exc,"HD review decision unavailable")
    return 200,{field:result[field] for field in ("ok","action","profile_id","hd_animal_instance_id","pending_confirmation","proposal_id","replayed") if field in result}


def handle_profile_reassignment(
    environ: Mapping[str, str], token: str, profile_id: str, *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    key = _signing_key(environ); current = int(time.time()) if epoch_now is None else int(epoch_now)
    event_id = _verify_aux_action_token(token, "reassign", key, current) if key is not None else None
    if event_id is None or not isinstance(profile_id, str) or _UUID.fullmatch(profile_id) is None:
        return 404, {"ok": False, "error": "not found"}
    url=environ.get("SUPABASE_URL",""); secret=environ.get("SUPABASE_SECRET_KEY","")
    if not url or not secret: return 404, {"ok": False, "error": "not found"}
    try:
        catalog=catalog_factory(url,secret,environ.get("SUPABASE_BUCKET","tactacam-photos")); clock=time.monotonic; catalog.set_deadline(clock()+LIBRARY_DEADLINE_SECONDS,clock=clock)
        result=catalog.reassign_hd_instance(event_id,profile_id)
        if not isinstance(result,Mapping) or not result.get("ok"): raise StorageError("Profile reassignment failed")
    except (AttributeError,OSError,RuntimeError,TypeError,ValueError,StorageError): return 503,{"ok":False,"error":"profile reassignment unavailable"}
    return 200,dict(result)


def handle_automation_label(
    environ: Mapping[str, str],
    token: str,
    verdict: str,
    note: str = "",
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    key = _signing_key(environ); current = int(time.time()) if epoch_now is None else int(epoch_now)
    event_id = _verify_aux_action_token(token, "audit", key, current) if key is not None else None
    if event_id is None or verdict not in {"correct", "should_have_requested_hd", "incorrect_male_or_antler"} or not isinstance(note, str) or len(note) > 500:
        return 404, {"ok": False, "error": "not found"}
    url = environ.get("SUPABASE_URL", ""); secret = environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not secret:
        return 404, {"ok": False, "error": "not found"}
    try:
        catalog = catalog_factory(url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos"))
        clock = time.monotonic; catalog.set_deadline(clock() + LIBRARY_DEADLINE_SECONDS, clock=clock)
        result = catalog.record_automation_label(event_id, verdict, note.strip())
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise StorageError("Automation label failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return 503, {"ok": False, "error": "automation label unavailable"}
    return 200, dict(result)


def handle_gate1b_label(
    environ: Mapping[str, str],
    token: str,
    species_label: str,
    visible_antler: str,
    probable_male: str,
    head_visibility: str,
    note: str = "",
    *,
    catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog,
    epoch_now: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Append one human correction without overwriting model evidence."""
    if species_label not in {"whitetail", "axis", "other_deer", "non_deer", "unknown"}:
        return 400, {"ok": False, "error": "invalid species label"}
    if visible_antler not in {"yes", "no", "unknown"}:
        return 400, {"ok": False, "error": "invalid antler label"}
    if probable_male not in {"yes", "no", "unknown"}:
        return 400, {"ok": False, "error": "invalid male label"}
    if head_visibility not in {"full", "partial", "none", "unknown"}:
        return 400, {"ok": False, "error": "invalid head visibility"}
    if not isinstance(note, str) or len(note) > 500:
        return 400, {"ok": False, "error": "invalid note"}
    key = _signing_key(environ)
    current = int(time.time()) if epoch_now is None else int(epoch_now)
    capability = _verify_review_token(token, key, current) if key is not None else None
    url = environ.get("SUPABASE_URL", "")
    secret = environ.get("SUPABASE_SECRET_KEY", "")
    if capability is None or not url or not secret:
        return 404, {"ok": False, "error": "not found"}
    media_id, assessment_id, review_version = capability
    try:
        catalog = catalog_factory(
            url, secret, environ.get("SUPABASE_BUCKET", "tactacam-photos")
        )
        clock = time.monotonic
        catalog.set_deadline(clock() + LIBRARY_DEADLINE_SECONDS, clock=clock)
        result = catalog.record_gate1b_label(
            media_id,
            assessment_id,
            review_version,
            species_label,
            visible_antler,
            probable_male,
            head_visibility,
            note.strip(),
        )
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise StorageError("Gate 1B correction failed")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StorageError):
        return 503, {"ok": False, "error": "label unavailable"}
    return 200, {"ok": True, "media_id": media_id, "label_id": result.get("label_id")}


def handle_photos_query(environ: Mapping[str, str], query: Mapping[str, str], *, catalog_factory: Callable[[str, str, str], Any] = SupabaseCatalog, epoch_now: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
    key = _signing_key(environ); url=environ.get("SUPABASE_URL",""); secret=environ.get("SUPABASE_SECRET_KEY","")
    if key is None or not url or not secret: return 404,{"ok":False,"error":"not found"}
    allowed={"cursor","date_from","date_to","hour_from","hour_to","time_of_day","camera_id","species","male_antler","profile_status","variant","identity_status","sort","limit"}
    if any(name not in allowed for name in query): return 400,{"ok":False,"error":"invalid filter"}
    sort=query.get("sort","newest"); tod=query.get("time_of_day","all")
    if sort not in {"newest","oldest","time_asc","time_desc"} or tod not in {"all","day","night"}: return 400,{"ok":False,"error":"invalid filter"}
    try:
        limit=max(1,min(60,int(query.get("limit","30"))))
        filters={name:(None if query.get(name) in (None,"") else query.get(name)) for name in allowed if name!="limit"}; filters.update({"limit":limit,"sort":sort,"time_of_day":tod})
        for field in ("hour_from","hour_to"):
            if filters.get(field) is not None:
                filters[field]=int(filters[field]);
                if not 0 <= filters[field] <= 23: raise ValueError
        camera_id=filters.get("camera_id")
        if camera_id is not None and _UUID.fullmatch(str(camera_id)) is None: raise ValueError
        catalog=catalog_factory(url,secret,environ.get("SUPABASE_BUCKET","tactacam-photos")); raw=catalog.query_all_photos(filters)
        if not isinstance(raw,Mapping) or not isinstance(raw.get("items"),list): raise StorageError("Photo query unavailable")
        current=int(time.time()) if epoch_now is None else int(epoch_now)
        return 200,{"ok":True,"items":_sanitize_photos(raw["items"],key,current,max_items=limit),"next_cursor":raw.get("next_cursor"),"total":max(0,int(raw.get("total",0))),"facets":raw.get("facets",{})}
    except (AttributeError,OSError,RuntimeError,TypeError,ValueError,StorageError): return 400,{"ok":False,"error":"invalid filter"}


def handle_profile_representative(environ: Mapping[str,str], token: str, profile_id: str, *, catalog_factory: Callable[[str,str,str],Any]=SupabaseCatalog, epoch_now: Optional[int]=None) -> Tuple[int,Dict[str,Any]]:
    key=_signing_key(environ); current=int(time.time()) if epoch_now is None else int(epoch_now)
    event_id=_verify_aux_action_token(token,"representative",key,current) if key else None
    url=environ.get("SUPABASE_URL",""); secret=environ.get("SUPABASE_SECRET_KEY","")
    if event_id is None or _UUID.fullmatch(profile_id or "") is None or not url or not secret: return 404,{"ok":False,"error":"not found"}
    try:
        result=catalog_factory(url,secret,environ.get("SUPABASE_BUCKET","tactacam-photos")).set_profile_representative(event_id,profile_id)
        if not isinstance(result,Mapping) or not result.get("ok"): raise StorageError("Representative unavailable")
        return 200,{"ok":True,"profile_id":profile_id,"assignment_event_id":event_id}
    except (AttributeError,OSError,RuntimeError,TypeError,ValueError,StorageError): return 503,{"ok":False,"error":"representative unavailable"}


def _sanitize_photos(value: Any, key: bytes, now: int, *, max_items: int = MAX_LIBRARY_PHOTOS) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > max_items:
        raise StorageError("Private library is unavailable")
    output = []
    allowed = {
        "id",
        "captured_at",
        "camera_id",
        "camera_name",
        "variant",
        "width",
        "height",
        "labels",
        "animals",
        "hd_photo",
        "has_headshot",
        "battery_level",
        "signal_level",
        "gate1",
        "gate1b",
        "review_decision",
    }
    for item in value:
        media_id = item.get("id") if isinstance(item, Mapping) else None
        if not isinstance(media_id, str) or _UUID.fullmatch(media_id) is None:
            raise StorageError("Private library is unavailable")
        safe = {name: item[name] for name in allowed if name in item}
        safe["preview_url"] = (
            f"/api/library_preview?token={_sign_media_token(media_id, now + LIBRARY_PREVIEW_SECONDS, key)}"
        )
        gate1 = safe.get("gate1")
        decision = safe.get("review_decision")
        if isinstance(gate1, Mapping):
            pending_hd = gate1.get("pending_hd", False)
            if not isinstance(pending_hd, bool):
                raise StorageError("Private library is unavailable")
        else:
            pending_hd = False
        gate1b = safe.get("gate1b")
        if gate1b is not None:
            if not isinstance(gate1b, Mapping):
                raise StorageError("Private library is unavailable")
            if gate1b.get("queue") not in {
                "likely_male",
                "uncertain",
                "female_audit",
                "suppressed",
            }:
                raise StorageError("Private library is unavailable")
            for field, choices in (
                (
                    "species_label",
                    {"whitetail", "axis", "other_deer", "non_deer", "unknown", None},
                ),
                ("visible_antler", {"yes", "no", "unknown", None}),
                ("probable_male", {"yes", "no", "unknown", None}),
                ("head_visibility", {"full", "partial", "none", "unknown", None}),
            ):
                if gate1b.get(field) not in choices:
                    raise StorageError("Private library is unavailable")
        if (
            isinstance(gate1, Mapping)
            and gate1.get("route") == "review"
            and (not isinstance(gate1b, Mapping) or gate1b.get("queue") != "suppressed")
            and not pending_hd
            and (
                decision is None
                or (isinstance(decision, Mapping) and decision.get("action") == "defer")
            )
        ):
            assessment_id = gate1.get("id")
            review_version = gate1.get("review_version")
            if (
                not isinstance(assessment_id, int)
                or assessment_id < 1
                or not isinstance(review_version, int)
                or review_version < 0
            ):
                raise StorageError("Private library is unavailable")
            safe["review_token"] = _sign_review_token(
                media_id,
                assessment_id,
                review_version,
                now + LIBRARY_REVIEW_SECONDS,
                key,
            )
        output.append(safe)
    return output


def _sign_asset_token(media_asset_id: str, expires_at: int, key: bytes) -> str:
    payload = f"asset.{media_asset_id}.{expires_at}"
    digest = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def _verify_asset_token(token: str, key: bytes, now: int) -> Optional[str]:
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 4 or parts[0] != "asset" or _UUID.fullmatch(parts[1]) is None or not parts[2].isdigit(): return None
    expires=int(parts[2]); expected=hmac.new(key,f"asset.{parts[1]}.{expires}".encode("ascii"),hashlib.sha256).hexdigest()
    return parts[1] if expires >= now and hmac.compare_digest(parts[3],expected) else None


def _sanitize_auxiliary_media_rows(value: Any, key: bytes, now: int, id_field: str) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 500:
        raise StorageError("Auxiliary review queue is unavailable")
    output: list[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise StorageError("Auxiliary review queue is unavailable")
        media_id = raw.get("media_id")
        row_id = raw.get(id_field)
        if not isinstance(media_id, str) or _UUID.fullmatch(media_id) is None or not isinstance(row_id, int) or isinstance(row_id, bool) or row_id < 1:
            raise StorageError("Auxiliary review queue is unavailable")
        safe = dict(raw)
        asset_id = raw.get("media_asset_id")
        token = _sign_asset_token(asset_id, now + LIBRARY_PREVIEW_SECONDS, key) if isinstance(asset_id, str) and _UUID.fullmatch(asset_id) else _sign_media_token(media_id, now + LIBRARY_PREVIEW_SECONDS, key)
        safe["preview_url"] = "/api/library_preview?token=" + token
        purpose = "audit" if id_field == "automation_event_id" else "hdreview"
        safe["action_token"] = _sign_aux_action_token(row_id, purpose, now + LIBRARY_REVIEW_SECONDS, key)
        proposal_id = raw.get("proposal_id")
        proposal_event_id = raw.get("proposal_event_id")
        if isinstance(proposal_id, str) and _UUID.fullmatch(proposal_id) and isinstance(proposal_event_id, int) and not isinstance(proposal_event_id, bool) and proposal_event_id > 0:
            safe["proposal_token"] = _sign_aux_action_token(proposal_event_id, "proposal", now + LIBRARY_REVIEW_SECONDS, key)
        output.append(safe)
    return output


def _sanitize_hd_review_queue_page(value: Any, key: bytes, now: int, limit: int) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > limit:
        raise StorageError("HD review queue is unavailable")
    output: list[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise StorageError("HD review queue is unavailable")
        result_id, instance_id, media_id, asset_id = raw.get("hd_review_result_id"), raw.get("hd_animal_instance_id"), raw.get("media_id"), raw.get("media_asset_id")
        if not isinstance(result_id, int) or isinstance(result_id, bool) or result_id < 1 or not isinstance(instance_id, str) or _UUID.fullmatch(instance_id) is None or not isinstance(media_id, str) or _UUID.fullmatch(media_id) is None or not isinstance(asset_id, str) or _UUID.fullmatch(asset_id) is None:
            raise StorageError("HD review queue is unavailable")
        analysis = raw.get("result")
        if not isinstance(analysis, Mapping) or len(json.dumps(analysis, separators=(",", ":"))) > 32768:
            raise StorageError("HD review queue is unavailable")
        analysis_fields = ("species","sex","summary","age_cues","view_angle","visible_tines_left","visible_tines_right","tine_count_limitations","antler_structure","age_class","identity_eligible","age_eligible","antler_score_eligible","antler_score_range","distinguishing_features")
        clean_analysis: Dict[str, Any] = {}
        for field in analysis_fields:
            candidate = analysis.get(field)
            if candidate is None or isinstance(candidate, (str, int, float, bool)):
                clean_analysis[field] = candidate
            elif isinstance(candidate, list) and len(candidate) <= 40 and all(item is None or isinstance(item, (str, int, float, bool)) for item in candidate):
                clean_analysis[field] = list(candidate)
        captured_at, created_at, camera_id = raw.get("captured_at"), raw.get("created_at"), raw.get("camera_id")
        if not isinstance(captured_at, str) or len(captured_at) > 64 or not isinstance(created_at, str) or len(created_at) > 64 or (camera_id is not None and (not isinstance(camera_id, str) or _UUID.fullmatch(camera_id) is None)):
            raise StorageError("HD review queue is unavailable")
        raw_history = raw.get("geometry_history") or []
        if not isinstance(raw_history, list) or len(raw_history) > 10:
            raise StorageError("HD review queue is unavailable")
        geometry_history = []
        for entry in raw_history:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), int) or not isinstance(entry.get("reason"), str) or not isinstance(entry.get("created_at"), str) or len(entry["created_at"]) > 64:
                raise StorageError("HD review queue is unavailable")
            geometry_history.append({"id":entry["id"],"reason":entry["reason"][:40],"note":str(entry.get("note") or "")[:500],"created_at":entry["created_at"],"bbox":_sanitize_bbox(entry.get("bbox") or {})})
        safe: Dict[str, Any] = {
            "hd_review_result_id": result_id, "hd_animal_instance_id": instance_id, "media_id": media_id, "media_asset_id": asset_id,
            "instance_index": int(raw.get("instance_index") or 0), "instance_count": int(raw.get("instance_count") or 0),
            "bbox": _sanitize_bbox(raw.get("bbox") or {}), "original_bbox": _sanitize_bbox(raw.get("original_bbox") or raw.get("bbox") or {}),
            "geometry_event_id": raw.get("geometry_event_id"), "geometry_history": geometry_history, "review_origin": raw.get("review_origin") or "model_detection", "analysis_status": raw.get("analysis_status") or "complete", "split_from_instance_id": raw.get("split_from_instance_id"), "detection_complete": raw.get("detection_complete") is True,
            "detection_notes": str(raw.get("detection_notes") or "")[:500], "model_name": str(raw.get("model_name") or "")[:120], "model_version": str(raw.get("model_version") or "")[:160],
            "result": clean_analysis, "created_at": created_at, "captured_at": captured_at, "camera_id": camera_id, "camera_name": str(raw.get("camera_name") or "")[:100],
            "workflow_state": raw.get("workflow_state"), "workflow_reason": raw.get("workflow_reason"), "workflow_note": raw.get("workflow_note"), "topology_event_id": raw.get("topology_event_id"), "proposal_state": raw.get("proposal_state"), "proposal_action": raw.get("proposal_action"), "proposed_profile_id": raw.get("proposed_profile_id"),
            "proposed_display_name": raw.get("proposed_display_name"), "proposal_id": raw.get("proposal_id"), "proposal_event_id": raw.get("proposal_event_id"),
        }
        if safe["instance_index"] < 1 or safe["instance_count"] < safe["instance_index"]:
            raise StorageError("HD review queue is unavailable")
        if safe["geometry_event_id"] is not None and (not isinstance(safe["geometry_event_id"], int) or isinstance(safe["geometry_event_id"], bool) or safe["geometry_event_id"] < 1):
            raise StorageError("HD review queue is unavailable")
        if safe["review_origin"] not in {"model_detection", "human_topology_correction"}:
            raise StorageError("HD review queue is unavailable")
        if safe["analysis_status"] not in {"complete", "not_run", "stale_geometry"} or (safe["split_from_instance_id"] is not None and (not isinstance(safe["split_from_instance_id"], str) or _UUID.fullmatch(safe["split_from_instance_id"]) is None)):
            raise StorageError("HD review queue is unavailable")
        if safe["topology_event_id"] is not None and (not isinstance(safe["topology_event_id"], int) or isinstance(safe["topology_event_id"], bool) or safe["topology_event_id"] < 1):
            raise StorageError("HD review queue is unavailable")
        if safe["workflow_state"] not in {None,"active","reopen","defer","detector_error"} or safe["proposal_state"] not in {None,"pending","confirmed","undone"} or safe["proposal_action"] not in {None,"create_profile","match_profile"}:
            raise StorageError("HD review queue is unavailable")
        if safe["workflow_reason"] not in {None,"wrong_deer","box_clipped","multiple_deer","missed_deer","inseparable","false_detection","other"} or (safe["workflow_note"] is not None and (not isinstance(safe["workflow_note"], str) or len(safe["workflow_note"]) > 500)):
            raise StorageError("HD review queue is unavailable")
        if safe["proposal_id"] is not None and (not isinstance(safe["proposal_id"], str) or _UUID.fullmatch(safe["proposal_id"]) is None):
            raise StorageError("HD review queue is unavailable")
        if safe["proposed_profile_id"] is not None and (not isinstance(safe["proposed_profile_id"], str) or _UUID.fullmatch(safe["proposed_profile_id"]) is None):
            raise StorageError("HD review queue is unavailable")
        if safe["proposed_display_name"] is not None and (not isinstance(safe["proposed_display_name"], str) or len(safe["proposed_display_name"]) > 80):
            raise StorageError("HD review queue is unavailable")
        safe["preview_url"] = "/api/library_preview?token=" + _sign_asset_token(asset_id, now + LIBRARY_PREVIEW_SECONDS, key)
        safe["action_token"] = _sign_hd_instance_action_token(result_id, instance_id, now + LIBRARY_REVIEW_SECONDS, key)
        if isinstance(safe["proposal_id"], str) and _UUID.fullmatch(safe["proposal_id"]) and isinstance(safe["proposal_event_id"], int) and not isinstance(safe["proposal_event_id"], bool) and safe["proposal_event_id"] > 0:
            safe["proposal_token"] = _sign_aux_action_token(safe["proposal_event_id"], "proposal", now + LIBRARY_REVIEW_SECONDS, key)
        output.append(safe)
    return output


def _sanitize_bbox(value: Mapping[str, Any]) -> Dict[str, float]:
    """Return one finite normalized box wholly contained by its source image."""
    box: Dict[str, float] = {}
    for field in ("x", "y", "width", "height"):
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise StorageError("Animal crop is unavailable")
        box[field] = float(raw)
    if box["x"] < 0 or box["y"] < 0 or box["width"] <= 0 or box["height"] <= 0:
        raise StorageError("Animal crop is unavailable")
    if box["x"] + box["width"] > 1.000001 or box["y"] + box["height"] > 1.000001:
        raise StorageError("Animal crop is unavailable")
    return box


def _sanitize_profiles(value: Any, key: bytes | None = None, now: int = 0) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 500:
        raise StorageError("Deer profiles are unavailable")
    profiles: list[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise StorageError("Deer profiles are unavailable")
        profile_id = raw.get("id")
        animal_id = raw.get("animal_id")
        display_name = raw.get("display_name")
        if (
            not isinstance(profile_id, str)
            or _UUID.fullmatch(profile_id) is None
            or not isinstance(animal_id, str)
            or _UUID.fullmatch(animal_id) is None
            or not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name) > 80
        ):
            raise StorageError("Deer profiles are unavailable")
        profile = {
                "id": profile_id,
                "animal_id": animal_id,
                "display_name": display_name.strip(),
                "species": str(raw.get("species") or "unknown")[:80],
                "sex": str(raw.get("sex") or "unknown")[:20],
                "season_year": int(raw.get("season_year") or 0),
                "photo_count": max(0, int(raw.get("photo_count") or 0)),
                "first_seen": raw.get("first_seen"),
                "last_seen": raw.get("last_seen"),
                "camera_ids": [value for value in list(raw.get("camera_ids") or []) if isinstance(value, str) and _UUID.fullmatch(value)],
                "camera_names": [str(value)[:100] for value in list(raw.get("camera_names") or []) if value],
                "representative_assignment_event_id": raw.get("representative_assignment_event_id"),
                "profile_crops": [],
            }
        previews=list(raw.get("profile_previews") or [])[:5]
        for preview in previews:
            if not isinstance(preview, Mapping):
                raise StorageError("Deer profiles are unavailable")
            asset_id=preview.get("media_asset_id");instance_id=preview.get("hd_animal_instance_id");bbox=preview.get("bbox")
            if key is None or not isinstance(asset_id,str) or _UUID.fullmatch(asset_id) is None or not isinstance(instance_id,str) or _UUID.fullmatch(instance_id) is None or not isinstance(bbox,Mapping):
                raise StorageError("Deer profiles are unavailable")
            crop={"preview_url":"/api/library_preview?token="+_sign_asset_token(asset_id,now+LIBRARY_PREVIEW_SECONDS,key),"bbox":_sanitize_bbox(bbox),"crop_recipe":preview.get("crop_recipe"),"hd_animal_instance_id":instance_id,"is_representative":preview.get("is_representative") is True}
            profile["profile_crops"].append(crop)
        representative=next((crop for crop in profile["profile_crops"] if crop["is_representative"]),None)
        if representative is not None:
            profile["representative_crop"]={field:representative[field] for field in ("preview_url","bbox","crop_recipe","hd_animal_instance_id")}
        profiles.append(profile)
    return profiles


def _sanitize_profile_gallery(
    value: Any,
    key: bytes,
    now: int,
    *,
    expected_profile_id: Optional[str] = None,
    limit: int = 60,
) -> list[Dict[str, Any]]:
    row_limit = max(1, min(60, int(limit)))
    if not isinstance(value, list) or len(value) > row_limit:
        raise StorageError("Profile gallery is unavailable")
    output = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise StorageError("Profile gallery is unavailable")
        profile_id, media_id = raw.get("animal_profile_id"), raw.get("media_id")
        if not isinstance(profile_id, str) or _UUID.fullmatch(profile_id) is None or not isinstance(media_id, str) or _UUID.fullmatch(media_id) is None:
            raise StorageError("Profile gallery is unavailable")
        if expected_profile_id is not None and profile_id != expected_profile_id:
            raise StorageError("Profile gallery is unavailable")
        captured_at, camera_id, camera_name = raw.get("captured_at"), raw.get("camera_id"), raw.get("camera_name")
        if not isinstance(captured_at, str) or not captured_at or len(captured_at) > 64:
            raise StorageError("Profile gallery is unavailable")
        if camera_id is not None and (not isinstance(camera_id, str) or _UUID.fullmatch(camera_id) is None):
            raise StorageError("Profile gallery is unavailable")
        if camera_name is not None and (not isinstance(camera_name, str) or len(camera_name) > 100):
            raise StorageError("Profile gallery is unavailable")
        crop_recipe = raw.get("crop_recipe")
        if crop_recipe is not None:
            if not isinstance(crop_recipe, Mapping):
                raise StorageError("Profile gallery is unavailable")
            try:
                if len(json.dumps(crop_recipe, separators=(",", ":"))) > 4096:
                    raise StorageError("Profile gallery is unavailable")
            except (TypeError, ValueError):
                raise StorageError("Profile gallery is unavailable")
            crop_recipe = dict(crop_recipe)
        safe = {"profile_id": profile_id, "media_id": media_id, "captured_at": captured_at, "camera_name": camera_name}
        asset_id = raw.get("media_asset_id")
        instance_id = raw.get("hd_animal_instance_id")
        event_id = raw.get("assignment_event_id")
        safe["camera_id"] = camera_id
        if not isinstance(asset_id, str) or _UUID.fullmatch(asset_id) is None or not isinstance(instance_id, str) or _UUID.fullmatch(instance_id) is None or not isinstance(raw.get("bbox"), Mapping) or not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
            raise StorageError("Profile gallery is unavailable")
        safe.update({"media_asset_id": asset_id, "hd_animal_instance_id": instance_id, "bbox": _sanitize_bbox(raw["bbox"]), "crop_recipe": crop_recipe, "preview_url": "/api/library_preview?token=" + _sign_asset_token(asset_id, now + LIBRARY_PREVIEW_SECONDS, key), "reassign_token": _sign_aux_action_token(event_id, "reassign", now + LIBRARY_REVIEW_SECONDS, key), "representative_token": _sign_aux_action_token(event_id, "representative", now + LIBRARY_REVIEW_SECONDS, key)})
        output.append(safe)
    return output


def _sanitize_cameras(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100:
        raise StorageError("Private camera map is unavailable")
    allowed = {
        "id",
        "name",
        "location_name",
        "latitude",
        "longitude",
        "observed_at",
        "battery_level",
        "signal_level",
        "hardware_version",
        "last_seen_at",
    }
    output = []
    for item in value:
        camera_id = item.get("id") if isinstance(item, Mapping) else None
        if not isinstance(camera_id, str) or _UUID.fullmatch(camera_id) is None:
            raise StorageError("Private camera map is unavailable")
        output.append({name: item[name] for name in allowed if name in item})
    return output


def _sanitize_pipeline(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageError("Gate 1 funnel is unavailable")
    model_name = value.get("model_name")
    model_version = value.get("model_version")
    if model_name != GATE1_MODEL_NAME or model_version != GATE1_MODEL_VERSION:
        raise StorageError("Gate 1 funnel is unavailable")
    count_fields = (
        "total_thumbnails",
        "assessed_thumbnails",
        "pending_thumbnails",
        "review_representatives",
        "event_duplicates",
        "archived",
        "blank_or_below_threshold",
        "confident_non_target",
        "unresolved_review",
        "resolved_review",
    )
    output: Dict[str, Any] = {"model_name": model_name, "model_version": model_version}
    for field in count_fields:
        count = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise StorageError("Gate 1 funnel is unavailable")
        output[field] = count
    if (
        output["assessed_thumbnails"] + output["pending_thumbnails"]
        != output["total_thumbnails"]
    ):
        raise StorageError("Gate 1 funnel is unavailable")
    return output


def _sanitize_process_overview(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("as_of"), str):
        raise StorageError("Process overview is unavailable")
    output: Dict[str, Any] = {"as_of": value["as_of"]}
    for window in ("last_24_hours", "last_7_days"):
        raw = value.get(window)
        if not isinstance(raw, Mapping) or not all(isinstance(raw.get(boundary), str) for boundary in ("from", "to")):
            raise StorageError("Process overview is unavailable")
        clean = {"from": raw["from"], "to": raw["to"]}
        for field in ("photos_received", "male_or_antler", "animal_crops", "hd_requests", "profiles"):
            count = raw.get(field)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise StorageError("Process overview is unavailable")
            clean[field] = count
        output[window] = clean
    return output


def _sanitize_pipeline_health(value: Any) -> Dict[str, Any]:
    stage_names = ("ingestion", "gate1", "gate1b", "hd_requests", "hd_returns", "hd_analysis", "profiling")
    if not isinstance(value, Mapping) or not isinstance(value.get("as_of"), str) or value.get("overall") not in {"healthy", "degraded", "error", "unknown"} or not isinstance(value.get("stages"), Mapping):
        raise StorageError("Pipeline health is unavailable")
    output: Dict[str, Any] = {"as_of": value["as_of"], "overall": value["overall"], "stages": {}}
    for name in stage_names:
        raw = value["stages"].get(name)
        if not isinstance(raw, Mapping) or not isinstance(raw.get("telemetry_complete"), bool):
            raise StorageError("Pipeline health is unavailable")
        clean: Dict[str, Any] = {"telemetry_complete": raw["telemetry_complete"]}
        for field in ("last_success_at", "oldest_pending_at"):
            candidate = raw.get(field)
            if candidate is not None and (not isinstance(candidate, str) or len(candidate) > 64):
                raise StorageError("Pipeline health is unavailable")
            clean[field] = candidate
        for field in ("pending_count", "stale_claim_count", "failure_count_24h"):
            candidate = raw.get(field)
            if candidate is not None and (not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 0):
                raise StorageError("Pipeline health is unavailable")
            clean[field] = candidate
        output["stages"][name] = clean
    return output


def _sanitize_operational_stats(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageError("Operational statistics are unavailable")
    output: Dict[str, Any] = {}
    for field in ("photos_received_24h", "hd_requests_24h", "hd_available_24h"):
        count = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise StorageError("Operational statistics are unavailable")
        output[field] = count
    as_of = value.get("as_of")
    if not isinstance(as_of, str) or not as_of:
        raise StorageError("Operational statistics are unavailable")
    output["as_of"] = as_of
    return output


def _sanitize_gate1b_metrics(value: Any) -> Dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or value.get("model_name") != GATE1B_MODEL_NAME
        or value.get("model_version") != GATE1B_MODEL_VERSION
    ):
        raise StorageError("Gate 1B metrics are unavailable")
    count_fields = (
        "predictions",
        "likely_male",
        "uncertain",
        "female_candidates",
        "human_labels",
        "labeled_buck_events",
        "labeled_cameras",
        "labeled_day",
        "labeled_ir",
        "labeled_axis",
        "minimum_labels",
        "minimum_buck_events",
    )
    output: Dict[str, Any] = {
        "model_name": value["model_name"],
        "model_version": value["model_version"],
    }
    for field in count_fields:
        count = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise StorageError("Gate 1B metrics are unavailable")
        output[field] = count
    for field in ("suppression_enabled", "suppression_ready"):
        if not isinstance(value.get(field), bool):
            raise StorageError("Gate 1B metrics are unavailable")
        output[field] = value[field]
    audit = value.get("female_audit_percent")
    recall_target = value.get("required_buck_recall")
    for key in (
        "prediction_cameras",
        "predicted_whitetail",
        "predicted_axis",
        "predicted_other_deer",
        "predicted_non_deer",
        "predicted_day",
        "predicted_ir",
        "predicted_mixed_groups",
    ):
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise StorageError("Gate 1B metric is invalid")
        output[key] = int(raw)
    recall = value.get("buck_recall")
    if not isinstance(audit, int) or isinstance(audit, bool) or not 1 <= audit <= 100:
        raise StorageError("Gate 1B metrics are unavailable")
    if (
        not isinstance(recall_target, (int, float))
        or isinstance(recall_target, bool)
        or not 0 <= recall_target <= 1
    ):
        raise StorageError("Gate 1B metrics are unavailable")
    if recall is not None and (
        not isinstance(recall, (int, float))
        or isinstance(recall, bool)
        or not 0 <= recall <= 1
    ):
        raise StorageError("Gate 1B metrics are unavailable")
    output.update(
        {
            "female_audit_percent": audit,
            "required_buck_recall": float(recall_target),
            "buck_recall": None if recall is None else float(recall),
        }
    )
    if (
        output["likely_male"] + output["uncertain"] + output["female_candidates"]
        != output["predictions"]
    ):
        raise StorageError("Gate 1B metrics are unavailable")
    if output["human_labels"] > output["predictions"]:
        raise StorageError("Gate 1B metrics are unavailable")
    return output


def _signing_key(environ: Mapping[str, str]) -> Optional[bytes]:
    secret = environ.get("LIBRARY_PREVIEW_SECRET", "")
    return secret.encode("utf-8") if len(secret) >= 16 else None


def _sign_media_token(media_id: str, expires: int, key: bytes) -> str:
    payload = f"{expires}.{media_id}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _sign_aux_action_token(row_id: int, purpose: str, expires: int, key: bytes) -> str:
    payload = f"{expires}.{purpose}.{row_id}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _sign_hd_instance_action_token(result_id: int, instance_id: str, expires: int, key: bytes) -> str:
    payload = f"{expires}.hdinstance.{result_id}.{instance_id}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify_hd_instance_action_token(token: str, key: bytes, now: int) -> Optional[Tuple[int, str]]:
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 5 or parts[1] != "hdinstance" or not parts[0].isdigit() or not parts[2].isdigit() or _UUID.fullmatch(parts[3]) is None:
        return None
    expires, result_id, instance_id = int(parts[0]), int(parts[2]), parts[3]
    if result_id < 1 or expires < now or expires > now + LIBRARY_REVIEW_SECONDS + 30:
        return None
    expected = hmac.new(key, f"{expires}.hdinstance.{result_id}.{instance_id}".encode("ascii"), hashlib.sha256).hexdigest()
    return (result_id, instance_id) if hmac.compare_digest(parts[4], expected) else None


def _verify_aux_action_token(token: str, purpose: str, key: bytes, now: int) -> Optional[int]:
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 4 or parts[1] != purpose or not parts[0].isdigit() or not parts[2].isdigit(): return None
    expires, row_id = int(parts[0]), int(parts[2])
    if row_id < 1 or expires < now or expires > now + LIBRARY_REVIEW_SECONDS + 30: return None
    expected = hmac.new(key, f"{expires}.{purpose}.{row_id}".encode("ascii"), hashlib.sha256).hexdigest()
    return row_id if hmac.compare_digest(parts[3], expected) else None


def _sign_review_token(
    media_id: str, assessment_id: int, review_version: int, expires: int, key: bytes
) -> str:
    payload = f"{expires}.review.{media_id}.{assessment_id}.{review_version}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify_review_token(
    token: str, key: bytes, now: int
) -> Optional[Tuple[str, int, int]]:
    if not isinstance(token, str) or len(token) > 210:
        return None
    parts = token.split(".")
    if len(parts) != 6:
        return None
    expires_text, purpose, media_id, assessment_text, version_text, supplied = parts
    if (
        purpose != "review"
        or not expires_text.isdigit()
        or _UUID.fullmatch(media_id) is None
        or not assessment_text.isdigit()
        or not version_text.isdigit()
        or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
    ):
        return None
    expires = int(expires_text)
    if expires < now or expires > now + LIBRARY_REVIEW_SECONDS + 30:
        return None
    assessment_id = int(assessment_text)
    review_version = int(version_text)
    if assessment_id < 1 or review_version < 0:
        return None
    payload = f"{expires}.review.{media_id}.{assessment_id}.{review_version}"
    expected = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return (
        (media_id, assessment_id, review_version)
        if hmac.compare_digest(expected, supplied)
        else None
    )


def _verify_media_token(token: str, key: bytes, now: int) -> Optional[str]:
    if not isinstance(token, str) or len(token) > 150:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    expires_text, media_id, supplied = parts
    if (
        not expires_text.isdigit()
        or _UUID.fullmatch(media_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
    ):
        return None
    expires = int(expires_text)
    if expires < now or expires > now + LIBRARY_PREVIEW_SECONDS + 30:
        return None
    payload = f"{expires}.{media_id}"
    expected = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return media_id if hmac.compare_digest(expected, supplied) else None


def _not_found() -> Tuple[int, str, bytes]:
    return 404, "application/json", b'{"error":"not found"}'
