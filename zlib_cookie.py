#!/usr/bin/env python3
"""zlib session (cookie) tools: status / backup / restore / import.

All operations are local. No password is read or stored by this script.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "zlib"
SESSION_FILE = CONFIG_DIR / "session.json"
ENV_FILE = CONFIG_DIR / ".env"
BACKUP_DIR = HERE / "session_backup"
COOKIE_FILE = HERE / "cookies.txt"          # Netscape import source
HAND_COOKIE = HERE / "cookie.txt"           # hand-editable key=value file
ZLIB_EXE = HERE / "zlib.exe"
DEFAULT_DOMAIN = "https://z-lib.gd"


def read_session():
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def cmd_status():
    sys.path.insert(0, str(HERE))
    try:
        import zlib_web
        cookies = zlib_web.read_cookie_file()
        source = "cookie.txt"
    except Exception:  # noqa: BLE001
        cookies, source = {}, "session.json"
        data = read_session()
        if data:
            cookies = data.get("cookies") or {}

    if not cookies:
        print("[!] NO COOKIE FOUND.")
        print("    Edit cookie.txt in this folder: fill remix_userid and remix_userkey.")
        return 1
    names = sorted(cookies)
    has_auth = "remix_userid" in names and "remix_userkey" in names
    print("Cookie file  : %s" % (HAND_COOKIE if source == "cookie.txt" else SESSION_FILE))
    print("Cookies (%d)  : %s" % (len(names), ", ".join(names)))
    print("Auth cookies : %s" % ("OK" if has_auth else "MISSING"))
    print("Domain       : auto-follow (any official domain works)")
    if has_auth:
        print("STATUS       : READY - you can search right now.")
        return 0
    print("STATUS       : INCOMPLETE - fill both values in cookie.txt.")
    return 1


def cmd_backup(quiet=False):
    data = read_session()
    if data is None:
        if not quiet:
            print("[!] Nothing to back up: not logged in.")
        return 1
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SESSION_FILE, BACKUP_DIR / "session.json")
    if ENV_FILE.exists():
        shutil.copy2(ENV_FILE, BACKUP_DIR / "domain.env")
    if not quiet:
        print("[OK] Session backed up to: %s" % BACKUP_DIR)
    return 0


def cmd_restore():
    src = BACKUP_DIR / "session.json"
    if not src.exists():
        print("[!] No backup found: %s" % src)
        return 1
    if SESSION_FILE.exists():
        shutil.copy2(SESSION_FILE, BACKUP_DIR / "session.json.bak")
        print("[i] Previous session saved as session.json.bak")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, SESSION_FILE)
    env_src = BACKUP_DIR / "domain.env"
    if env_src.exists():
        shutil.copy2(env_src, ENV_FILE)
    print("[OK] Session restored from: %s" % BACKUP_DIR)
    return cmd_status()


def cmd_import(cookie_path=None, domain=None):
    path = Path(cookie_path or str(COOKIE_FILE)).expanduser()
    if not path.is_absolute():
        path = HERE / path
    if not path.exists():
        print("[!] Cookie file not found: %s" % path)
        print("    Put the Netscape/Mozilla cookies.txt next to zlib.exe,")
        print("    or pass its full path as the first argument.")
        return 1
    target = (domain or DEFAULT_DOMAIN).strip()
    if read_session() is not None:
        cmd_backup(quiet=True)
        print("[i] Current session backed up before import.")
    print("Importing cookies from: %s" % path)
    print("Target domain        : %s" % target)
    proc = subprocess.run(
        [str(ZLIB_EXE), "login", "--cookies", str(path), "--domain", target],
        cwd=str(HERE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print("[!] Import failed (exit %d)." % proc.returncode)
        print("    Restore the old session with option 2 if needed.")
        return proc.returncode
    print("[OK] Import finished. Current status:")
    return cmd_status()


def cmd_domains():
    """Show which official domain answers right now, and optionally pin one."""
    sys.path.insert(0, str(HERE))
    try:
        import zlib_web
    except Exception as exc:  # noqa: BLE001
        print("[!] Cannot load zlib_web.py: %s" % exc)
        return 1
    if len(sys.argv) > 2 and sys.argv[2].lower() == "pin":
        if len(sys.argv) < 4:
            print("Usage: zlib_cookie.py domains pin <https://domain>")
            return 2
        value = sys.argv[3].strip().rstrip("/")
        if not value.startswith("https://"):
            value = "https://" + value
        zlib_web.DOMAIN_OVERRIDE.write_text(value + "\n", encoding="utf-8")
        print("[OK] Pinned domain: %s" % value)
        return 0
    if len(sys.argv) > 2 and sys.argv[2].lower() == "auto":
        if zlib_web.DOMAIN_OVERRIDE.exists():
            zlib_web.DOMAIN_OVERRIDE.unlink()
            print("[OK] Removed pin. Domains will auto-follow.")
        else:
            print("[i] No pin to remove.")
        return 0

    data = read_session()
    if data is None:
        print("[!] Not logged in.")
        return 1
    cookies = data.get("cookies") or {}
    print("Pinned domain : %s" % (
        zlib_web.DOMAIN_OVERRIDE.read_text(encoding="utf-8-sig").strip()
        if zlib_web.DOMAIN_OVERRIDE.exists() else "(auto)"
    ))
    print("Last working  : %s" % zlib_web._last_domain())
    print("Probing official domains (one request each, please wait)...")
    live = []
    for domain in zlib_web.KNOWN_DOMAINS[:8]:
        try:
            zlib_web._http_get(domain + "/eapi/info/domains", cookies, 6)
            live.append(domain)
            print("  OK   %s" % domain)
        except Exception as exc:  # noqa: BLE001
            print("  --   %s (%s)" % (domain, type(exc).__name__))
    if live:
        print("[OK] %d domain(s) reachable: %s" % (len(live), ", ".join(live)))
        print("     Search will auto-pick a working one. No re-login needed.")
        return 0
    print("[!] No domain reachable right now. Try again later or check network/proxy.")
    return 1


def main():
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if action in ("status", "s"):
        return cmd_status()
    if action in ("backup", "b"):
        return cmd_backup()
    if action in ("restore", "r"):
        return cmd_restore()
    if action in ("import", "i"):
        cookie = sys.argv[2] if len(sys.argv) > 2 else None
        domain = sys.argv[3] if len(sys.argv) > 3 else None
        return cmd_import(cookie, domain)
    if action in ("domains", "d"):
        return cmd_domains()
    print("Usage: zlib_cookie.py [status|backup|restore|import|domains]")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
