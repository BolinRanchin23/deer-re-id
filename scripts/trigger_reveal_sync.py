#!/usr/bin/env python3
"""Silently dispatch the bounded DeerID REVEAL production sweep."""

import subprocess
import sys


command = [
    "gh",
    "workflow",
    "run",
    "329286703",
    "--repo",
    "BolinRanchin23/deer-re-id",
    "--ref",
    "main",
    "-f",
    "start_page=0",
    "-f",
    "page_count=5",
]
result = subprocess.run(command, capture_output=True, text=True)
if result.returncode:
    message = result.stderr.strip() or result.stdout.strip() or "unknown gh failure"
    print(f"DeerID REVEAL sync dispatch failed: {message}", file=sys.stderr)
    raise SystemExit(result.returncode)
