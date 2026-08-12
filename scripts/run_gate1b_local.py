#!/usr/bin/env python3
"""Quiet local scheduler entry point for the self-hosted Gate 1B model."""

import fcntl
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_REF = "vypmpmlhuqwvrxypowqa"
PROJECT_URL = f"https://{PROJECT_REF}.supabase.co"
REPO = Path(__file__).resolve().parents[1]
MODEL = "gemma4:e4b"
MODEL_DIGEST = "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb"


def has_pinned_model(tags: object) -> bool:
    if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("name") == MODEL
        and item.get("digest") == MODEL_DIGEST
        for item in tags["models"]
    )


def main() -> int:
    lock = open("/tmp/deerid-gate1b.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/tags", timeout=5
        ) as response:
            tags = json.load(response)
        if not has_pinned_model(tags):
            raise RuntimeError(
                f"required local model {MODEL}@{MODEL_DIGEST} is unavailable"
            )

        keys = subprocess.run(
            [
                "supabase",
                "projects",
                "api-keys",
                "--project-ref",
                PROJECT_REF,
                "--output",
                "json",
            ],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        records = json.loads(keys.stdout)
        service_key = next(
            item["api_key"]
            for item in records
            if item.get("name") == "service_role" and item.get("api_key")
        )
        environ = {
            **os.environ,
            "SUPABASE_URL": PROJECT_URL,
            "SUPABASE_SECRET_KEY": service_key,
            "SUPABASE_BUCKET": "tactacam-photos",
            "GATE1B_OLLAMA_URL": "http://127.0.0.1:11434",
            "GATE1B_OLLAMA_MODEL": MODEL,
        }
        completed = subprocess.run(
            [sys.executable, "-m", "reveal_downloader.gate1b_worker", "--limit", "20"],
            cwd=REPO,
            env=environ,
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
        )
        result = json.loads(completed.stdout)
        if result.get("failed"):
            raise RuntimeError(
                f"Gate 1B left {result['failed']} event(s) pending after model failure"
            )
        return 0
    except Exception as exc:
        print(f"DeerID Gate 1B scheduler failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
