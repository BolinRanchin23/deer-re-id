"""Server-managed Supabase email/password authentication."""
from __future__ import annotations

from http.cookies import SimpleCookie
import json
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib import error, parse, request


Transport = Callable[[str, str, Mapping[str, str], Optional[bytes]], Tuple[int, bytes, Mapping[str, str]]]
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{10,8192}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_RESPONSE = 64 * 1024
_RECOVERY_DECOY = "deerid-recovery-probe@invalid.example"


def _config(environ: Mapping[str, str]) -> Optional[Tuple[str, str]]:
    base = environ.get("SUPABASE_URL", "").rstrip("/")
    key = environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    parsed = parse.urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or len(key) < 20:
        return None
    return base, key


def valid_auth_request(
    environ: Mapping[str, str],
    *,
    origin: Optional[str],
    content_type: Optional[str],
    fetch_site: Optional[str],
) -> bool:
    """Require browser auth mutations to be same-origin JSON requests."""
    expected = parse.urlparse(environ.get("PUBLIC_SITE_URL", ""))
    supplied = parse.urlparse(origin or "")
    if expected.scheme != "https" or not expected.netloc:
        return False
    if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
        return False
    if not (content_type or "").lower().startswith("application/json"):
        return False
    return not fetch_site or fetch_site.lower() == "same-origin"


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
) -> Tuple[int, bytes, Mapping[str, str]]:
    req = request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with request.urlopen(req, timeout=12) as response:
            data = response.read(_MAX_RESPONSE + 1)
            if len(data) > _MAX_RESPONSE:
                return 502, b"{}", {}
            return response.status, data, dict(response.headers.items())
    except error.HTTPError as exc:
        return exc.code, exc.read(_MAX_RESPONSE), dict(exc.headers.items())
    except (error.URLError, TimeoutError, OSError):
        return 503, b"{}", {}


def _json_body(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _parse_json(data: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _headers(key: str, access_token: Optional[str] = None) -> Dict[str, str]:
    headers = {"apikey": key, "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _valid_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_TOKEN_RE.fullmatch(value))


def _session_cookies(data: Mapping[str, Any]) -> List[str]:
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not _valid_token(access) or not _valid_token(refresh):
        return []
    try:
        expires = max(60, min(int(data.get("expires_in", 3600)), 3600))
    except (TypeError, ValueError):
        expires = 3600
    attributes = "Path=/api/; HttpOnly; Secure; SameSite=Strict"
    return [
        f"deerid_access={access}; Max-Age={expires}; {attributes}",
        f"deerid_refresh={refresh}; Max-Age=2592000; {attributes}",
    ]


def clear_session_cookies() -> List[str]:
    attributes = "Path=/api/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
    return [f"deerid_access=; {attributes}", f"deerid_refresh=; {attributes}"]


def _cookie_value(cookie_header: Optional[str], name: str) -> Optional[str]:
    if not cookie_header or len(cookie_header) > 20_000:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(name)
    value = morsel.value if morsel else None
    return value if _valid_token(value) else None


def _public_user(value: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    user_id = value.get("id")
    email = value.get("email")
    if not isinstance(user_id, str) or not user_id or not isinstance(email, str):
        return None
    return {"id": user_id, "email": email.lower()}


def _user_allowed(environ: Mapping[str, str], user: Optional[Mapping[str, str]]) -> bool:
    configured = environ.get("AUTH_ALLOWED_EMAILS", "")
    allowed = {
        value.strip().lower()
        for value in configured.split(",")
        if value.strip() and len(value.strip()) <= 254
    }
    return bool(user and user.get("email", "").lower() in allowed)


def _email_allowed(environ: Mapping[str, str], email: str) -> bool:
    return _user_allowed(environ, {"email": email})


def handle_auth_action(
    environ: Mapping[str, str],
    payload: Mapping[str, Any],
    *,
    transport: Transport = _default_transport,
) -> Tuple[int, Dict[str, Any], List[str]]:
    config = _config(environ)
    if config is None:
        return 404, {"ok": False, "error": "not found"}, []
    base, key = config
    action = payload.get("action")
    email: Any = None

    if action == "logout":
        return 200, {"ok": True}, clear_session_cookies()

    if action in ("login", "recover"):
        email = payload.get("email")
        if not isinstance(email, str) or len(email) > 254 or not _EMAIL_RE.fullmatch(email):
            return 400, {"ok": False, "error": "valid email required"}, []

    if action == "login":
        password = payload.get("password")
        if not isinstance(password, str) or not password or len(password) > 1024:
            return 400, {"ok": False, "error": "password required"}, []
        status, raw, _ = transport(
            "POST",
            f"{base}/auth/v1/token?grant_type=password",
            _headers(key),
            _json_body({"email": email, "password": password}),
        )
        data = _parse_json(raw)
        cookies = _session_cookies(data) if status == 200 else []
        user = _public_user(data.get("user", {})) if status == 200 else None
        if not cookies or user is None or not _user_allowed(environ, user):
            return 401, {"ok": False, "error": "Invalid email or password"}, []
        return 200, {"ok": True, "user": user}, cookies

    if action == "recover":
        recovery_email = email if _email_allowed(environ, email) else _RECOVERY_DECOY
        redirect = environ.get("PUBLIC_SITE_URL", "https://deer-re-id.vercel.app").rstrip("/") + "/"
        transport(
            "POST",
            f"{base}/auth/v1/recover?redirect_to={parse.quote(redirect, safe=':/')}",
            _headers(key),
            _json_body({"email": recovery_email}),
        )
        return 200, {"ok": True, "message": "If the account exists, a recovery email was sent."}, []

    if action == "update_password":
        password = payload.get("password")
        token = payload.get("access_token")
        if not isinstance(password, str) or len(password) < 8:
            return 400, {"ok": False, "error": "password must be at least 8 characters"}, []
        if len(password) > 1024 or not _valid_token(token):
            return 400, {"ok": False, "error": "invalid password setup request"}, []
        status, raw, _ = transport(
            "PUT",
            f"{base}/auth/v1/user",
            _headers(key, token),
            _json_body({"password": password}),
        )
        user_data = _parse_json(raw)
        user = _public_user(user_data.get("user", user_data)) if status == 200 else None
        if status != 200 or not _user_allowed(environ, user):
            return 401, {"ok": False, "error": "password setup link is invalid or expired"}, []
        return 200, {"ok": True}, []

    return 400, {"ok": False, "error": "unsupported action"}, []


def authenticate_session(
    environ: Mapping[str, str],
    cookie_header: Optional[str],
    *,
    transport: Transport = _default_transport,
) -> Tuple[int, Optional[Dict[str, str]], List[str]]:
    config = _config(environ)
    if config is None:
        return 404, None, []
    base, key = config
    access = _cookie_value(cookie_header, "deerid_access")
    refresh = _cookie_value(cookie_header, "deerid_refresh")
    if access:
        status, raw, _ = transport("GET", f"{base}/auth/v1/user", _headers(key, access), None)
        user = _public_user(_parse_json(raw)) if status == 200 else None
        if _user_allowed(environ, user):
            return 200, user, []
    if refresh:
        status, raw, _ = transport(
            "POST",
            f"{base}/auth/v1/token?grant_type=refresh_token",
            _headers(key),
            _json_body({"refresh_token": refresh}),
        )
        data = _parse_json(raw)
        cookies = _session_cookies(data) if status == 200 else []
        user = _public_user(data.get("user", {})) if status == 200 else None
        if cookies and _user_allowed(environ, user):
            return 200, user, cookies
    return 401, None, clear_session_cookies()
