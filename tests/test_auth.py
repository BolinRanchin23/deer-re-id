import json
import unittest

from reveal_downloader.auth import (
    authenticate_session,
    handle_auth_action,
    valid_auth_request,
)


class FakeAuthTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0)


def response(status, payload):
    return status, json.dumps(payload).encode("utf-8"), {}


class SupabaseAuthTests(unittest.TestCase):
    def setUp(self):
        self.environ = {
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public-project-key",
            "AUTH_ALLOWED_EMAILS": "owner@example.com",
        }

    def test_password_login_returns_identity_and_httponly_cookies_without_tokens(self):
        transport = FakeAuthTransport([
            response(200, {
                "access_token": "header.payload.signature",
                "refresh_token": "refresh-token-value",
                "expires_in": 3600,
                "user": {"id": "user-1", "email": "owner@example.com"},
            })
        ])
        status, payload, cookies = handle_auth_action(
            self.environ,
            {"action": "login", "email": "owner@example.com", "password": "strong-password"},
            transport=transport,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "user": {"id": "user-1", "email": "owner@example.com"}})
        self.assertNotIn("access_token", json.dumps(payload))
        self.assertEqual(len(cookies), 2)
        for cookie in cookies:
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertIn("Path=/api/", cookie)
        self.assertIn("grant_type=password", transport.calls[0][1])
        self.assertEqual(transport.calls[0][2]["apikey"], self.environ["SUPABASE_PUBLISHABLE_KEY"])

    def test_authenticated_but_unlisted_email_is_rejected(self):
        transport = FakeAuthTransport([
            response(200, {
                "access_token": "header.payload.signature",
                "refresh_token": "refresh-token-value",
                "expires_in": 3600,
                "user": {"id": "user-2", "email": "stranger@example.com"},
            })
        ])
        status, payload, cookies = handle_auth_action(
            self.environ,
            {"action": "login", "email": "stranger@example.com", "password": "long-password"},
            transport=transport,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"ok": False, "error": "Invalid email or password"})
        self.assertEqual(cookies, [])

    def test_invalid_login_is_generic_and_sets_no_cookie(self):
        transport = FakeAuthTransport([response(400, {"error_description": "Invalid login credentials"})])
        status, payload, cookies = handle_auth_action(
            self.environ,
            {"action": "login", "email": "owner@example.com", "password": "wrong-password"},
            transport=transport,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"ok": False, "error": "Invalid email or password"})
        self.assertEqual(cookies, [])

    def test_session_validation_uses_httponly_access_cookie(self):
        transport = FakeAuthTransport([response(200, {"id": "user-1", "email": "owner@example.com"})])
        status, user, cookies = authenticate_session(
            self.environ,
            "deerid_access=header.payload.signature; deerid_refresh=refresh-token-value",
            transport=transport,
        )
        self.assertEqual(status, 200)
        self.assertEqual(user["email"], "owner@example.com")
        self.assertEqual(cookies, [])
        self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer header.payload.signature")

    def test_expired_access_cookie_is_refreshed_server_side(self):
        transport = FakeAuthTransport([
            response(401, {"message": "JWT expired"}),
            response(200, {
                "access_token": "new.header.signature",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
                "user": {"id": "user-1", "email": "owner@example.com"},
            }),
        ])
        status, user, cookies = authenticate_session(
            self.environ,
            "deerid_access=old.header.signature; deerid_refresh=old-refresh-token",
            transport=transport,
        )
        self.assertEqual(status, 200)
        self.assertEqual(user["id"], "user-1")
        self.assertEqual(len(cookies), 2)
        self.assertIn("grant_type=refresh_token", transport.calls[1][1])

    def test_recovery_sends_email_without_revealing_account_existence(self):
        transport = FakeAuthTransport([response(200, {})])
        status, payload, cookies = handle_auth_action(
            self.environ,
            {"action": "recover", "email": "owner@example.com"},
            transport=transport,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "message": "If the account exists, a recovery email was sent."})
        self.assertEqual(cookies, [])

    def test_disallowed_recovery_still_performs_same_outbound_request_shape(self):
        transport = FakeAuthTransport([response(200, {})])
        status, payload, _ = handle_auth_action(
            self.environ,
            {"action": "recover", "email": "stranger@example.com"},
            transport=transport,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(transport.calls), 1)
        body = json.loads(transport.calls[0][3])
        self.assertNotEqual(body["email"], "stranger@example.com")
        self.assertEqual(payload["message"], "If the account exists, a recovery email was sent.")

    def test_auth_mutations_require_same_origin_json(self):
        environ = {**self.environ, "PUBLIC_SITE_URL": "https://deer-re-id.vercel.app"}
        self.assertTrue(valid_auth_request(
            environ,
            origin="https://deer-re-id.vercel.app",
            content_type="application/json; charset=utf-8",
            fetch_site="same-origin",
        ))
        self.assertFalse(valid_auth_request(
            environ,
            origin="https://evil.example",
            content_type="application/json",
            fetch_site="cross-site",
        ))
        self.assertFalse(valid_auth_request(
            environ,
            origin="https://deer-re-id.vercel.app",
            content_type="text/plain",
            fetch_site="same-origin",
        ))

    def test_password_update_requires_eight_characters(self):
        status, payload, cookies = handle_auth_action(
            self.environ,
            {"action": "update_password", "access_token": "recovery.jwt.token", "password": "1234"},
            transport=FakeAuthTransport([]),
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "password must be at least 8 characters")
        self.assertEqual(cookies, [])


if __name__ == "__main__":
    unittest.main()
