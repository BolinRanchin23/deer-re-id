import tempfile
import unittest
from pathlib import Path

from reveal_downloader.cli import build_parser, run
from reveal_downloader.client import RevealError


class FakeClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password

    def get_photos(self, *, size, page, camera_id=None):
        if page:
            return []
        return [
            {
                "photoId": "p1",
                "cameraId": "c1",
                "photoDateUtc": "2026-08-06T12:00:00Z",
                "photoUrl": "https://example.test/p1.jpg",
            }
        ]

    def download(self, url):
        return b"\xff\xd8photo\xff\xd9"


class CliTests(unittest.TestCase):
    def test_sync_command_prompts_for_password_and_downloads(self):
        created = []

        def factory(username, password):
            created.append(FakeClient(username, password))
            return created[-1]

        with tempfile.TemporaryDirectory() as tmp:
            exit_code = run(
                ["sync", "--username", "person@example.com", "--output", tmp],
                client_factory=factory,
                password_reader=lambda prompt: "prompted-secret",
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(created[0].password, "prompted-secret")
            self.assertEqual(len(list(Path(tmp).rglob("*.jpg"))), 1)

    def test_sync_rejects_invalid_pagination_values(self):
        parser = build_parser()
        invalid_arguments = (
            ["sync", "--username", "person@example.com", "--page-size", "0"],
            ["sync", "--username", "person@example.com", "--max-pages", "-1"],
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                parser.parse_args(arguments)

    def test_watch_continues_after_transient_reveal_failure(self):
        class FlakyClient(FakeClient):
            calls = 0

            def get_photos(self, *, size, page, camera_id=None):
                self.calls += 1
                if self.calls == 1:
                    raise RevealError("temporary outage")
                return []

        client = FlakyClient("person@example.com", "secret")
        sleeps = []

        def sleeper(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp:
            exit_code = run(
                [
                    "watch",
                    "--username",
                    "person@example.com",
                    "--output",
                    tmp,
                    "--interval",
                    "1",
                ],
                client_factory=lambda username, password: client,
                password_reader=lambda prompt: "secret",
                sleeper=sleeper,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.calls, 2)
        self.assertEqual(sleeps, [1, 1])


if __name__ == "__main__":
    unittest.main()
