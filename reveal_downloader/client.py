"""Synchronous client for the unofficial Tactacam Reveal cloud API."""

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

COGNITO_URL = "https://cognito-idp.us-east-1.amazonaws.com/"
COGNITO_CLIENT_ID = "6r9tpojvgvkci5trla0ip14mon"
API_BASE_URL = "https://api.reveal.ishareit.net/v1"
USER_AGENT = "RevealWeb/5.4.0"


class RevealError(RuntimeError):
    """Base error raised by the Reveal client."""


class AuthenticationError(RevealError):
    """Authentication failed or requires an unsupported challenge."""


class HttpTransport:
    """Small urllib transport so the downloader has no third-party dependency."""

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
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RevealError(f"HTTP {exc.code} from Reveal API: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RevealError(f"Could not reach Reveal API: {exc}") from exc

    def bytes_request(self, url: str) -> bytes:
        try:
            with urlopen(Request(url), timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            raise RevealError(f"Image download failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RevealError(f"Image download failed: {exc}") from exc


class RevealClient:
    """Authenticate and read cameras/photos from the Reveal cloud service."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        transport: Optional[Any] = None,
    ) -> None:
        self.username = username
        self._password = password
        self._transport = transport or HttpTransport()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None

    def authenticate(self) -> None:
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
        auth = response.get("AuthenticationResult")
        if not auth:
            challenge = response.get("ChallengeName")
            if challenge:
                raise AuthenticationError(
                    f"Reveal requested unsupported login challenge: {challenge}"
                )
            raise AuthenticationError("Reveal rejected the username or password")
        self._access_token = auth.get("AccessToken")
        self._refresh_token = auth.get("RefreshToken")
        expires_in = int(auth.get("ExpiresIn", 43200))
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if not self._access_token:
            raise AuthenticationError("Reveal login returned no access token")

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
        auth = response.get("AuthenticationResult")
        if not auth or not auth.get("AccessToken"):
            self.authenticate()
            return
        self._access_token = auth["AccessToken"]
        expires_in = int(auth.get("ExpiresIn", 43200))
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
        return response.get("response", {}).get("photos", [])

    def get_cameras(self) -> List[Dict[str, Any]]:
        response = self._transport.json_request(
            "GET", f"{API_BASE_URL}/cameras", headers=self._headers()
        )
        return response.get("response", {}).get("cameras", [])

    def download(self, url: str) -> bytes:
        return self._transport.bytes_request(url)
