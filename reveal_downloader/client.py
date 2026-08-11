"""Synchronous client for the unofficial Tactacam Reveal cloud API."""

from datetime import datetime, timedelta, timezone
import http.client
import ipaddress
import json
import math
import queue
import re
import socket
import ssl
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

COGNITO_URL = "https://cognito-idp.us-east-1.amazonaws.com/"
COGNITO_CLIENT_ID = "6r9tpojvgvkci5trla0ip14mon"
API_BASE_URL = "https://api.reveal.ishareit.net/v1"
USER_AGENT = "RevealWeb/5.4.0"
TRUSTED_PHOTO_HOST_SUFFIX = ".reveal.ishareit.net"
TRUSTED_REVEAL_S3_HOST = re.compile(
    r"ftp-us-east-1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.s3\.us-east-1\.amazonaws\.com"
)
MAX_TOKEN_LIFETIME_SECONDS = 7 * 24 * 60 * 60
DEFAULT_HTTP_TIMEOUT = 8.0
MIN_REQUEST_BUDGET = 0.1


class RevealError(RuntimeError):
    """Base error raised by the Reveal client."""


class AuthenticationError(RevealError):
    """Authentication failed or requires an unsupported challenge."""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP peer is fixed while TLS verifies ``host``."""

    def __init__(self, host: str, address: str, timeout: float) -> None:
        self._tls_context = ssl.create_default_context()
        super().__init__(host, 443, timeout=timeout, context=self._tls_context)
        self._address = address

    def connect(self) -> None:
        parsed = ipaddress.ip_address(self._address)
        family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        destination = (
            (self._address, 443, 0, 0)
            if family == socket.AF_INET6
            else (self._address, 443)
        )
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(destination)
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class HttpTransport:
    """Small urllib transport so the downloader has no third-party dependency."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._deadline: Optional[float] = None

    def set_deadline(
        self,
        deadline: Optional[float],
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._deadline = deadline
        if clock is not None:
            self._clock = clock

    def _timeout(self) -> float:
        if self._deadline is None:
            return DEFAULT_HTTP_TIMEOUT
        remaining = self._deadline - self._clock()
        if remaining < MIN_REQUEST_BUDGET:
            raise RevealError("Reveal request deadline reached")
        return min(DEFAULT_HTTP_TIMEOUT, remaining)

    def json_request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if params:
            url = f"{url}?{urlencode(params)}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=self._timeout()) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RevealError(f"HTTP {exc.code} from Reveal API: {detail}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RevealError("Reveal API returned invalid JSON") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RevealError(f"Could not reach Reveal API: {exc}") from exc

    def bytes_request(self, url: str) -> bytes:
        _validate_photo_url(url)
        parts = urlsplit(url)
        hostname_value = parts.hostname
        if hostname_value is None:  # Defensive; _validate_photo_url rejects this.
            raise RevealError("Photo URL has no hostname")
        hostname = hostname_value.rstrip(".").lower()
        address = _resolve_global_address(hostname, self._timeout())
        connection = _PinnedHTTPSConnection(hostname, address, self._timeout())
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        try:
            connection.request("GET", target, headers={"Host": hostname})
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                raise RevealError(
                    f"Image download failed with HTTP {response.status}"
                )
            return response.read()
        except RevealError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise RevealError(f"Image download failed: {exc}") from exc
        finally:
            connection.close()


class RevealClient:
    """Authenticate and read cameras/photos from the Reveal cloud service."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        transport: Optional[Any] = None,
        allow_unsafe_download_urls: bool = False,
    ) -> None:
        self.username = username
        self._password = password
        self._transport = transport or HttpTransport()
        self._allow_unsafe_download_urls = allow_unsafe_download_urls
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None

    def set_deadline(
        self,
        deadline: Optional[float],
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        setter = getattr(self._transport, "set_deadline", None)
        if callable(setter):
            setter(deadline, clock=clock)

    def authenticate(self) -> None:
        try:
            response = self._transport.json_request(
                "POST",
                COGNITO_URL,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
                    "X-Amz-User-Agent": "aws-amplify/6.8.2 auth/4 framework/1",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Origin": "https://account.revealcellcam.com",
                    "Referer": "https://account.revealcellcam.com/",
                },
                payload={
                    "AuthFlow": "USER_PASSWORD_AUTH",
                    "AuthParameters": {"USERNAME": self.username, "PASSWORD": self._password},
                    "ClientId": COGNITO_CLIENT_ID,
                },
            )
        except RevealError as exc:
            raise AuthenticationError(f"Reveal authentication request failed: {exc}") from exc
        if not isinstance(response, dict):
            raise AuthenticationError("Reveal login returned a malformed response")
        auth = response.get("AuthenticationResult")
        if not auth:
            challenge = response.get("ChallengeName")
            if challenge:
                raise AuthenticationError(
                    f"Reveal requested unsupported login challenge: {challenge}"
                )
            raise AuthenticationError("Reveal rejected the username or password")
        access_token, refresh_token, expires_in = _validated_auth_result(response)
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    def _ensure_authenticated(self) -> None:
        if not self._access_token or not self._expires_at:
            self.authenticate()
            return
        if datetime.now(timezone.utc) >= self._expires_at - timedelta(minutes=5):
            if self._refresh_token:
                self._refresh()
            else:
                self.authenticate()

    def _refresh(self) -> None:
        try:
            response = self._transport.json_request(
                "POST",
                COGNITO_URL,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
                    "X-Amz-User-Agent": "aws-amplify/6.8.2 auth/4 framework/1",
                },
                payload={
                    "AuthFlow": "REFRESH_TOKEN_AUTH",
                    "AuthParameters": {"REFRESH_TOKEN": self._refresh_token},
                    "ClientId": COGNITO_CLIENT_ID,
                },
            )
        except RevealError as exc:
            raise AuthenticationError(f"Reveal token refresh failed: {exc}") from exc
        access_token, refresh_token, expires_in = _validated_auth_result(response)
        self._access_token = access_token
        if refresh_token is not None:
            self._refresh_token = refresh_token
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    def _headers(self) -> Dict[str, str]:
        self._ensure_authenticated()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "reveal-user-agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Origin": "https://account.revealcellcam.com",
            "Referer": "https://account.revealcellcam.com/",
        }

    def get_photos(
        self,
        *,
        size: int = 100,
        page: int = 0,
        camera_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "size": size,
            "page": page,
            "includeWeatherData": "true",
        }
        if camera_id:
            params["cameraId"] = camera_id
        response = self._transport.json_request(
            "GET",
            f"{API_BASE_URL}/photos",
            headers=self._headers(),
            params=params,
        )
        return [_normalize_photo_timestamp(photo) for photo in _response_items(response, "photos")]

    def get_cameras(self) -> List[Dict[str, Any]]:
        response = self._transport.json_request(
            "GET", f"{API_BASE_URL}/cameras", headers=self._headers()
        )
        return _response_items(response, "cameras")

    def download(self, url: str) -> bytes:
        if not self._allow_unsafe_download_urls:
            _validate_photo_url(url)
        body = self._transport.bytes_request(url)
        if not (
            (len(body) >= 4 and body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9"))
            or body.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise RevealError("Photo download did not contain a plausible JPEG or PNG image")
        return body


def _resolve_global_address(hostname: str, timeout: float) -> str:
    """Resolve once within ``timeout`` and return a vetted address."""
    results = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            answer = socket.getaddrinfo(
                hostname, 443, type=socket.SOCK_STREAM
            )
        except BaseException as exc:
            results.put(exc)
        else:
            results.put(answer)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        answer = results.get(timeout=timeout)
    except queue.Empty as exc:
        raise RevealError("Photo URL host resolution timed out") from exc
    if isinstance(answer, BaseException):
        raise RevealError("Photo URL host could not be resolved safely") from answer
    addresses = []
    for item in answer:
        address = item[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise RevealError("Photo URL host returned an invalid address") from exc
        if not parsed.is_global:
            raise RevealError("Photo URL must not resolve to a private or local address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise RevealError("Photo URL host did not resolve")
    return addresses[0]


def _response_items(response: Any, key: str) -> List[Dict[str, Any]]:
    if not isinstance(response, dict):
        raise RevealError("Reveal API returned a malformed response")
    envelope = response.get("response")
    if not isinstance(envelope, dict):
        raise RevealError("Reveal API returned a malformed response")
    items = envelope.get(key)
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise RevealError("Reveal API returned a malformed response")
    return items


def _normalize_photo_timestamp(photo: Dict[str, Any]) -> Dict[str, Any]:
    """Fill REVEAL's occasionally omitted UTC photo date from its UTC creation epoch."""
    existing = photo.get("photoDateUtc")
    if isinstance(existing, str) and existing.strip():
        return photo
    created = photo.get("createdTimestamp")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return photo
    try:
        derived = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return photo
    if not 2000 <= derived.year <= 2100:
        return photo
    normalized = dict(photo)
    normalized["photoDateUtc"] = derived.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    if normalized["photoDateUtc"].endswith(".000Z"):
        normalized["photoDateUtc"] = normalized["photoDateUtc"].replace(".000Z", "Z")
    return normalized


def _valid_token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _validated_auth_result(response: Any):
    if not isinstance(response, dict):
        raise AuthenticationError("Reveal authentication returned a malformed response")
    auth = response.get("AuthenticationResult")
    if not isinstance(auth, dict):
        raise AuthenticationError("Reveal authentication returned malformed tokens")
    access_token = auth.get("AccessToken")
    refresh_token = auth.get("RefreshToken")
    expires_in = auth.get("ExpiresIn")
    if not _valid_token(access_token):
        raise AuthenticationError("Reveal authentication returned a malformed access token")
    if refresh_token is not None and not _valid_token(refresh_token):
        raise AuthenticationError("Reveal authentication returned a malformed refresh token")
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or not math.isfinite(expires_in)
        or expires_in <= 0
        or expires_in > MAX_TOKEN_LIFETIME_SECONDS
    ):
        raise AuthenticationError("Reveal authentication returned an invalid expiry")
    return access_token, refresh_token, expires_in


def _validate_photo_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise RevealError("Photo URL must be an HTTPS URL without embedded credentials")
    try:
        explicit_port = parts.port is not None
    except ValueError as exc:
        raise RevealError("Photo URL has an invalid port") from exc
    hostname = parts.hostname.rstrip(".").lower()
    if explicit_port:
        raise RevealError("Photo URL must use the default HTTPS port")
    if not (
        hostname == TRUSTED_PHOTO_HOST_SUFFIX[1:]
        or hostname.endswith(TRUSTED_PHOTO_HOST_SUFFIX)
        or TRUSTED_REVEAL_S3_HOST.fullmatch(hostname)
    ):
        raise RevealError("Photo URL host is not a trusted Reveal host")
