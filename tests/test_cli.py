import tempfile
import unittest
from pathlib import Path

from reveal_downloader.cli import run


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
        return b"photo"


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


if __name__ == "__main__":
    unittest.main()
