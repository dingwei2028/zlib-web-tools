#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Text-mode login helper for zlib.exe (no TUI form needed)."""
from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ZLIB_EXE = ROOT / "zlib.exe"
DEFAULT_DOMAIN = "https://z-lib.gd"


def main() -> int:
    if not ZLIB_EXE.exists():
        print(f"Not found: {ZLIB_EXE}", file=sys.stderr)
        return 2

    dry_run = "--dry-run" in sys.argv
    domain = input(f"Domain [{DEFAULT_DOMAIN}]: ").strip() or DEFAULT_DOMAIN
    email = input("Email: ").strip()
    if sys.stdin.isatty():
        password = getpass.getpass("Password (hidden): ")
    else:
        # Non-interactive/test runner: no console to hide input.
        password = input("Password (plain, no console): ")

    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        return 2

    args = [str(ZLIB_EXE), "login", "--domain", domain, "--email", email, "--password", password]
    if dry_run:
        safe = list(args)
        safe[-1] = "***"
        print("Would run:", " ".join(safe))
        return 0

    print("Logging in...")
    proc = subprocess.run(args, cwd=str(ROOT))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
