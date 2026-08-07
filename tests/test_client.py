import unittest
from datetime import datetime, timedelta, timezone

from reveal_downloader.client import RevealClient


class FakeTransport:
    def __init__(self):
        self.calls = []

    def json_request(self, method, url, *, headers=None, payload=None, params=None):
        self.calls.append((method, url, headers or {}, payload, params))
        if "cognito-idp" in url:
            if payload["AuthFlow"] == "REFRESH_TOKEN_AUTH":
                return {
                    "AuthenticationResult": {
                        "AccessToken": "refreshed-access-token",
                        "ExpiresIn": 3600,
                    }
                }
            return {
                "AuthenticationResult": {
                    "AccessToken": "access-token",
                    "IdToken": "id-token",
                    "RefreshToken": "refresh-token",
                    "ExpiresIn": 3600,
                }
            }
        return {"response": {"photos": [{"photoId": "p1"}]}}

    def bytes_request(self, url):
        return b"image"


class RevealClientTests(unittest.TestCase):
    def test_get_photos_authenticates_and_sends_access_token(self):
        transport = FakeTransport()
        client = RevealClient("person@example.com", "secret", transport=transport)

        photos = client.get_photos(size=25, page=2, camera_id="cam-1")

        self.assertEqual(photos, [{"photoId": "p1"}])
        api_call = transport.calls[-1]
        self.assertEqual(api_call[2]["Authorization"], "Bearer access-token")
        self.assertEqual(
            api_call[4],
            {"size": 25, "page": 2, "includeWeatherData": "true", "cameraId": "cam-1"},
        )

    def test_get_photos_refreshes_an_expired_access_token(self):
        transport = FakeTransport()
        client = RevealClient("person@example.com", "secret", transport=transport)
        client.authenticate()
        client._expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        client.get_photos()

        auth_flows = [
            call[3]["AuthFlow"]
            for call in transport.calls
            if "cognito-idp" in call[1]
        ]
        self.assertEqual(auth_flows, ["USER_PASSWORD_AUTH", "REFRESH_TOKEN_AUTH"])
        self.assertEqual(
            transport.calls[-1][2]["Authorization"],
            "Bearer refreshed-access-token",
        )


if __name__ == "__main__":
    unittest.main()
