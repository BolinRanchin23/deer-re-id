import socket
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from reveal_downloader.client import (
    AuthenticationError,
    HttpTransport,
    RevealClient,
    RevealError,
    _PinnedHTTPSConnection,
)


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
    def test_download_allows_reveal_managed_s3_photo_host_only(self):
        class ImageTransport(FakeTransport):
            def bytes_request(self, url):
                return b"\xff\xd8image\xff\xd9"

        client = RevealClient("person@example.com", "secret", transport=ImageTransport())
        reveal_s3_url = (
            "https://ftp-us-east-1-1373ee1d-b093-4e43-bde8-5f2a21b88d7d."
            "s3.us-east-1.amazonaws.com/photo.jpg"
        )

        self.assertEqual(client.download(reveal_s3_url), b"\xff\xd8image\xff\xd9")
        for url in (
            "https://attacker-bucket.s3.us-east-1.amazonaws.com/photo.jpg",
            "https://ftp-us-east-1-not-a-uuid.s3.us-east-1.amazonaws.com/photo.jpg",
            "https://ftp-us-east-1-1373ee1d-b093-4e43-bde8-5f2a21b88d7d.s3.eu-west-1.amazonaws.com/photo.jpg",
        ):
            with self.subTest(url=url), self.assertRaises(RevealError):
                client.download(url)

    def test_pinned_connection_uses_numeric_address_without_dns_lookup(self):
        connection = _PinnedHTTPSConnection(
            "images.reveal.ishareit.net", "93.184.216.34", 1.0
        )
        raw_socket = patch("reveal_downloader.client.socket.socket").start()
        self.addCleanup(patch.stopall)
        socket_instance = raw_socket.return_value
        connection._tls_context = patch(
            "reveal_downloader.client.ssl.create_default_context"
        ).start().return_value
        connection._tls_context.wrap_socket.return_value = socket_instance

        with patch(
            "reveal_downloader.client.socket.getaddrinfo",
            side_effect=AssertionError("pinned connect must not resolve DNS again"),
        ):
            connection.connect()

        raw_socket.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        socket_instance.settimeout.assert_called_once_with(1.0)
        socket_instance.connect.assert_called_once_with(("93.184.216.34", 443))
        connection._tls_context.wrap_socket.assert_called_once_with(
            socket_instance, server_hostname="images.reveal.ishareit.net"
        )

    def test_http_transport_network_calls_are_bounded_for_vercel(self):
        class JsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        class ImageResponse:
            status = 200

            def read(self):
                return b"image"

        class Connection:
            def __init__(self, hostname, address, timeout):
                self.timeout = timeout
                connections.append(self)

            def request(self, method, target, headers):
                pass

            def getresponse(self):
                return ImageResponse()

            def close(self):
                pass

        connections = []
        with patch("reveal_downloader.client.urlopen", return_value=JsonResponse()) as mocked, patch(
            "reveal_downloader.client.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ), patch("reveal_downloader.client._PinnedHTTPSConnection", Connection):
            transport = HttpTransport()
            transport.json_request("GET", "https://api.example.test/data")
            transport.bytes_request(
                "https://images.reveal.ishareit.net/photo.jpg"
            )

        self.assertLessEqual(mocked.call_args.kwargs["timeout"], 8)
        self.assertLessEqual(connections[0].timeout, 8)

    def test_http_transport_uses_remaining_deadline_and_refuses_tiny_budget(self):
        now = [10.0]
        transport = HttpTransport(clock=lambda: now[0])
        transport.set_deadline(12.0)
        with patch("reveal_downloader.client.urlopen") as mocked:
            response = mocked.return_value.__enter__.return_value
            response.read.return_value = b"{}"
            transport.json_request("GET", "https://api.reveal.ishareit.net/v1/photos")
            self.assertEqual(mocked.call_args.kwargs["timeout"], 2.0)

            now[0] = 11.95
            with self.assertRaisesRegex(RevealError, "deadline"):
                transport.json_request("GET", "https://api.reveal.ishareit.net/v1/photos")

        self.assertEqual(mocked.call_count, 1)

    def test_client_deadline_propagates_through_auth_and_photo_request(self):
        class DeadlineTransport(FakeTransport):
            def set_deadline(self, deadline, clock=None):
                self.deadline = deadline
                self.clock = clock

        clock = lambda: 10.0
        transport = DeadlineTransport()
        client = RevealClient("person@example.com", "secret", transport=transport)
        client.set_deadline(20.0, clock=clock)

        client.get_photos()

        self.assertEqual(transport.deadline, 20.0)
        self.assertIs(transport.clock, clock)
        self.assertEqual(len(transport.calls), 2)

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

    def test_get_photos_derives_missing_utc_date_from_guarded_epoch_timestamp(self):
        class MissingUtcTransport(FakeTransport):
            def json_request(self, method, url, *, headers=None, payload=None, params=None):
                if "cognito-idp" in url:
                    return super().json_request(
                        method, url, headers=headers, payload=payload, params=params
                    )
                return {
                    "response": {
                        "photos": [
                            {
                                "photoId": "p1",
                                "createdTimestamp": 1767225600000,
                                "photoTimestamp": "12312025185920",
                            },
                            {
                                "photoId": "p2",
                                "photoDateUtc": "2025-12-31T23:59:00Z",
                                "createdTimestamp": 1767225600000,
                            },
                            {"photoId": "p3", "createdTimestamp": "invalid"},
                        ]
                    }
                }

        client = RevealClient("person@example.com", "secret", transport=MissingUtcTransport())

        photos = client.get_photos()

        self.assertEqual(photos[0]["photoDateUtc"], "2026-01-01T00:00:00Z")
        self.assertEqual(photos[0]["createdTimestamp"], 1767225600000)
        self.assertEqual(photos[1]["photoDateUtc"], "2025-12-31T23:59:00Z")
        self.assertNotIn("photoDateUtc", photos[2])

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

    def test_download_rejects_unsafe_photo_urls(self):
        transport = FakeTransport()
        client = RevealClient("person@example.com", "secret", transport=transport)

        for url in (
            "file:///etc/passwd",
            "http://images.example.com/photo.jpg",
            "https://127.0.0.1/photo.jpg",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/photo.jpg",
        ):
            with self.subTest(url=url), self.assertRaises(RevealError):
                client.download(url)

    def test_download_only_allows_default_port_on_reveal_hosts(self):
        class ImageTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.byte_calls = []

            def bytes_request(self, url):
                self.byte_calls.append(url)
                return b"\xff\xd8image\xff\xd9"

        transport = ImageTransport()
        client = RevealClient("person@example.com", "secret", transport=transport)
        with patch(
            "reveal_downloader.client.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            self.assertEqual(
                client.download("https://images.reveal.ishareit.net/photo.jpg"),
                b"\xff\xd8image\xff\xd9",
            )
            for url in (
                "https://attacker.example/photo.jpg",
                "https://images.reveal.ishareit.net:443/photo.jpg",
                "https://images.reveal.ishareit.net:8443/photo.jpg",
                "https://reveal.ishareit.net.evil.example/photo.jpg",
            ):
                with self.subTest(url=url), self.assertRaises(RevealError):
                    client.download(url)

        self.assertEqual(
            transport.byte_calls, ["https://images.reveal.ishareit.net/photo.jpg"]
        )

    def test_http_transport_pins_the_single_dns_result_for_image_connection(self):
        class Response:
            status = 200

            def read(self):
                return b"image"

        class Connection:
            def __init__(self, hostname, address, timeout):
                self.arguments = (hostname, address, timeout)
                connections.append(self)

            def request(self, method, target, headers):
                self.request_arguments = (method, target, headers)

            def getresponse(self):
                return Response()

            def close(self):
                pass

        connections = []
        answers = [
            [(2, 1, 6, "", ("93.184.216.34", 443))],
            [(2, 1, 6, "", ("127.0.0.1", 443))],
        ]
        with patch(
            "reveal_downloader.client.socket.getaddrinfo", side_effect=answers
        ) as resolver, patch(
            "reveal_downloader.client._PinnedHTTPSConnection", Connection
        ):
            body = HttpTransport().bytes_request(
                "https://images.reveal.ishareit.net/photo.jpg?size=full"
            )

        self.assertEqual(body, b"image")
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(
            connections[0].arguments[:2],
            ("images.reveal.ishareit.net", "93.184.216.34"),
        )
        self.assertEqual(
            connections[0].request_arguments[:2], ("GET", "/photo.jpg?size=full")
        )
        self.assertEqual(
            connections[0].request_arguments[2]["Host"],
            "images.reveal.ishareit.net",
        )

    def test_http_transport_bounds_slow_dns_resolution_by_deadline(self):
        release = threading.Event()

        def slow_resolver(*args, **kwargs):
            release.wait(2)
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        transport = HttpTransport()
        transport.set_deadline(time.monotonic() + 0.15)
        started = time.monotonic()
        try:
            with patch(
                "reveal_downloader.client.socket.getaddrinfo",
                side_effect=slow_resolver,
            ), self.assertRaisesRegex(RevealError, "resol"):
                transport.bytes_request(
                    "https://images.reveal.ishareit.net/photo.jpg"
                )
        finally:
            release.set()

        self.assertLess(time.monotonic() - started, 0.75)

    def test_photo_download_disables_automatic_redirects(self):
        class Response:
            status = 302

            def read(self):
                return b""

        class Connection:
            def __init__(self, hostname, address, timeout):
                self.requests = []
                connections.append(self)

            def request(self, method, target, headers):
                self.requests.append((method, target, headers))

            def getresponse(self):
                return Response()

            def close(self):
                pass

        connections = []
        with patch(
            "reveal_downloader.client.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ), patch("reveal_downloader.client._PinnedHTTPSConnection", Connection):
            with self.assertRaisesRegex(RevealError, "HTTP 302"):
                HttpTransport().bytes_request(
                    "https://images.reveal.ishareit.net/photo.jpg"
                )

        self.assertEqual(len(connections), 1)
        self.assertEqual(len(connections[0].requests), 1)

    def test_download_rejects_non_image_response_bodies(self):
        client = RevealClient(
            "person@example.com",
            "secret",
            transport=FakeTransport(),
            allow_unsafe_download_urls=True,
        )

        with self.assertRaisesRegex(RevealError, "JPEG or PNG"):
            client.download("http://127.0.0.1/fake.jpg")

    def test_get_photos_normalizes_malformed_response_shape(self):
        class MalformedTransport(FakeTransport):
            def json_request(self, *args, **kwargs):
                if "cognito-idp" in args[1]:
                    return super().json_request(*args, **kwargs)
                return {"response": {"photos": "not-a-list"}}

        client = RevealClient("person@example.com", "secret", transport=MalformedTransport())

        with self.assertRaisesRegex(RevealError, "malformed"):
            client.get_photos()

    def test_http_transport_normalizes_malformed_json(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"<html>not json</html>"

        with patch("reveal_downloader.client.urlopen", return_value=Response()):
            with self.assertRaisesRegex(RevealError, "invalid JSON"):
                HttpTransport().json_request("GET", "https://api.example.test/data")

    def test_authentication_transport_failures_are_authentication_errors(self):
        class FailingTransport:
            def json_request(self, *args, **kwargs):
                raise RevealError("HTTP 401 from Reveal API")

        client = RevealClient("person@example.com", "wrong", transport=FailingTransport())

        with self.assertRaises(AuthenticationError):
            client.authenticate()

    def test_authentication_rejects_malformed_tokens_and_expiry(self):
        malformed = (
            {"AuthenticationResult": []},
            {"AuthenticationResult": {"AccessToken": 123, "RefreshToken": "r", "ExpiresIn": 3600}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": 123, "ExpiresIn": 3600}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "r", "ExpiresIn": True}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "r", "ExpiresIn": "3600"}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "r", "ExpiresIn": 0}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "r", "ExpiresIn": 999999999}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "r", "ExpiresIn": float("nan")}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "r", "ExpiresIn": float("inf")}},
            {"AuthenticationResult": {"AccessToken": "   ", "RefreshToken": "r", "ExpiresIn": 3600}},
            {"AuthenticationResult": {"AccessToken": "a\n", "RefreshToken": "r", "ExpiresIn": 3600}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "\t", "ExpiresIn": 3600}},
            {"AuthenticationResult": {"AccessToken": "a", "RefreshToken": "r\x00", "ExpiresIn": 3600}},
        )

        for response in malformed:
            class MalformedTransport:
                def json_request(self, *args, **kwargs):
                    return response

            with self.subTest(response=response), self.assertRaises(AuthenticationError):
                RevealClient("person@example.com", "secret", transport=MalformedTransport()).authenticate()

    def test_refresh_normalizes_malformed_response_and_transport_failure(self):
        outcomes = (
            {"AuthenticationResult": "not-a-dict"},
            {"AuthenticationResult": {"AccessToken": [], "ExpiresIn": 3600}},
            RevealError("refresh failed"),
        )
        for outcome in outcomes:
            class RefreshTransport:
                def json_request(self, *args, **kwargs):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

            client = RevealClient("person@example.com", "secret", transport=RefreshTransport())
            client._access_token = "old"
            client._refresh_token = "refresh"
            client._expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

            with self.subTest(outcome=outcome), self.assertRaises(AuthenticationError):
                client.get_photos()


if __name__ == "__main__":
    unittest.main()
