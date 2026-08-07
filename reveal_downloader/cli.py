"""Command-line interface for the standalone Reveal downloader."""

import argparse
from getpass import getpass
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, List, Optional

from .archive import PhotoArchive
from .client import AuthenticationError, RevealClient, RevealError


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reveal-downloader",
        description="Download Tactacam Reveal cloud photos into a durable local archive.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_login_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--username",
            default=os.environ.get("TACTACAM_USERNAME"),
            required=not bool(os.environ.get("TACTACAM_USERNAME")),
            help="Reveal account email (or set TACTACAM_USERNAME)",
        )

    def add_sync_arguments(command: argparse.ArgumentParser) -> None:
        add_login_arguments(command)
        command.add_argument("--output", type=Path, default=Path("reveal-archive"))
        command.add_argument("--camera-id")
        command.add_argument("--page-size", type=_positive_int, default=100)
        command.add_argument(
            "--max-pages",
            type=_nonnegative_int,
            default=0,
            help="Maximum pages per run; 0 downloads until the API returns no more",
        )

    sync_parser = subparsers.add_parser("sync", help="Download all currently available photos")
    add_sync_arguments(sync_parser)

    watch_parser = subparsers.add_parser("watch", help="Continuously check for new photos")
    add_sync_arguments(watch_parser)
    watch_parser.add_argument("--interval", type=int, default=300, help="Seconds between checks")

    cameras_parser = subparsers.add_parser("cameras", help="List cameras on the account")
    add_login_arguments(cameras_parser)
    return parser


def run(
    argv: Optional[List[str]] = None,
    *,
    client_factory: Callable[[str, str], Any] = RevealClient,
    password_reader: Callable[[str], str] = getpass,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    password = os.environ.get("TACTACAM_PASSWORD") or password_reader(
        "Tactacam Reveal password: "
    )
    if not password:
        print("A password is required.", file=sys.stderr)
        return 2

    client = client_factory(args.username, password)
    try:
        if args.command == "cameras":
            cameras = client.get_cameras()
            for camera in cameras:
                camera_id = camera.get("cameraId", "unknown")
                name = (
                    camera.get("cameraName")
                    or camera.get("cameraLocation")
                    or camera.get("name")
                    or "Unnamed camera"
                )
                print(f"{camera_id}\t{name}")
            return 0

        archive = PhotoArchive(args.output)
        if args.command == "sync":
            return _sync_once(archive, client, args)

        while True:
            try:
                _sync_once(archive, client, args)
            except AuthenticationError:
                raise
            except RevealError as exc:
                print(f"Transient Reveal API error: {exc}; retrying.", file=sys.stderr)
            sleeper(max(1, args.interval))
    except KeyboardInterrupt:
        print("Stopped.")
        return 0
    except AuthenticationError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 3
    except RevealError as exc:
        print(f"Reveal API error: {exc}", file=sys.stderr)
        return 4


def _sync_once(archive: PhotoArchive, client: Any, args: argparse.Namespace) -> int:
    result = archive.sync(
        client,
        camera_id=args.camera_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    print(
        f"Sync complete: downloaded={result.downloaded} "
        f"skipped={result.skipped} failed={result.failed} output={archive.root}"
    )
    return 1 if result.failed else 0


def main() -> None:
    raise SystemExit(run())
