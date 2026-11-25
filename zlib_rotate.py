#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-account quota-rotation downloader for heartleo/zlib.

Each Z-Library account gets a fixed daily download limit. This tool reads a
list of your own accounts, checks each one's remaining quota before a download,
and automatically switches to the next account with quota left.

Everything runs locally. No password is stored or sent anywhere except the
Z-Library EAPI itself.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent
ZLIB_EXE = ROOT / "zlib.exe"
ACCOUNTS_FILE = ROOT / "accounts.txt"
STATE_FILE = ROOT / ".rotate_state.json"
LINKS_FILE = ROOT / "download_links.txt"
LAST_DOMAIN_FILE = ROOT / ".last_domain"
DOWNLOAD_LOG = ROOT / ".download_log.jsonl"


def _system_downloads_dir() -> Path:
    """Resolve the Windows 'Downloads' folder as shown in File Explorer.

    Reads the shell's User Shell Folders registry value so a redirected
    Downloads folder is honoured. Falls back to ~/Downloads.
    """
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            raw, _ = winreg.QueryValueEx(
                key, "{374DE290-123F-4565-9164-39C4925E467B}"
            )
            raw = os.path.expandvars(str(raw)).strip()
            if raw:
                path = Path(raw)
                if not path.is_absolute():
                    path = (Path.home() / path).resolve()
                path.mkdir(parents=True, exist_ok=True)
                return path
        except Exception:  # noqa: BLE001
            pass
    fallback = Path.home() / "Downloads"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return fallback


DOWNLOAD_DIR = _system_downloads_dir()

SIMPLE_UA = "Mozilla/5.0"
KNOWN_DOMAINS = [
    "https://z-lib.gd", "https://z-lib.gl", "https://z-library.ec",
    "https://zlib.bz", "https://z-lib.by", "https://z-library.qa",
    "https://z-library.website", "https://1lib.sk", "https://article.sk",
    "https://articles.sk", "https://z-lib.fm", "https://z-lib.sk",
    "https://z-library.im", "https://z-library.sk",
]

# Domains that answered recently are reused first (avoids proxy churn).
_live_domains: list[str] = []


def parse_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        return []
    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[|\t,]", line) if p.strip()]
        if len(parts) < 3:
            continue
        accounts.append({"email": parts[0], "userid": parts[1], "userkey": parts[2]})
    return accounts


def _headers(account: dict) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": SIMPLE_UA,
        "remix-userid": account["userid"],
        "remix-userkey": account["userkey"],
        "Cookie": f"remix_userid={account['userid']}; remix_userkey={account['userkey']}",
    }


def _get(path: str, account: dict, timeout: int = 12):
    """GET across domains, reusing known-live ones first."""
    domains = list(dict.fromkeys(_live_domains + KNOWN_DOMAINS))
    last = None
    for domain in domains:
        try:
            req = urllib.request.Request(domain + path, headers=_headers(account), method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if domain not in _live_domains:
                    _live_domains.insert(0, domain)
                return json.loads(resp.read().decode("utf-8", "replace")), domain
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    raise RuntimeError(f"no reachable domain: {last}")


def get_quota(account: dict) -> dict:
    """Return {used, limit, remaining, email} for an account."""
    payload, _ = _get("/eapi/user/profile", account)
    user = payload.get("user") or {}
    used = user.get("downloads_today", user.get("dailyDownloadsCount", 0))
    limit = user.get("downloads_limit", user.get("dailyDownloadLimit", 10))
    try:
        used, limit = int(used), int(limit)
    except (TypeError, ValueError):
        used, limit = 0, 0
    return {
        "email": user.get("email") or account["email"],
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used) if limit else None,
    }


def get_download_link(account: dict, book_ref: str) -> tuple[str, str]:
    """Resolve an id:hash to a real download URL. Returns (url, domain)."""
    if ":" not in book_ref:
        raise ValueError("book id must be in id:hash format")
    book_id, book_hash = book_ref.split(":", 1)
    path = f"/eapi/book/{quote(book_id)}/{quote(book_hash)}/file"
    payload, domain = _get(path, account)
    if payload.get("success") != 1:
        raise RuntimeError(payload.get("error") or "file request rejected")
    link = (
        (payload.get("file") or {}).get("downloadLink")
        or payload.get("downloadLink")
        or payload.get("url")
        or payload.get("link")
    )
    if not link:
        raise RuntimeError("no download link in response")
    return link, domain


def _open_url(req, timeout: int, direct: bool):
    """Open URL; direct=True bypasses any system proxy (good for file CDNs)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
    return opener.open(req, timeout=timeout)


def _file_headers(account: dict, origin_domain: str) -> dict[str, str]:
    """Headers for the CDN file request (mirror the Go client's behavior).

    Key points from zlib's download.go: Accept must be text/html (not JSON),
    a Referer to the mirror is required, and account cookies go only to the
    mirror's own origin - a signed CDN link must NOT get remix credentials.
    """
    return {
        "User-Agent": SIMPLE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": origin_domain + "/",
    }


def fix_mojibake(name: str) -> str:
    """Repair filenames where UTF-8 bytes were decoded as Latin-1.

    Python's urllib decodes HTTP headers with Latin-1, so a UTF-8 filename in
    Content-Disposition shows up as 'åµä¹...'. Re-encoding to Latin-1 and
    decoding as UTF-8 restores the original text. Safe for plain ASCII and for
    already-correct Unicode (encode fails -> name returned unchanged).
    """
    if not name:
        return name
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def header_filename(disp: str) -> str:
    """Extract a filename from Content-Disposition, handling UTF-8 properly."""
    # RFC 5987 form: filename*=UTF-8''%E4%BA%BA... (already URL-encoded).
    star = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disp, re.IGNORECASE)
    if star:
        from urllib.parse import unquote

        return unquote(star.group(1).strip().strip('"'))
    plain = re.search(r'filename\s*=\s*"([^"]*)"', disp, re.IGNORECASE)
    if not plain:
        plain = re.search(r"filename\s*=\s*([^;]+)", disp, re.IGNORECASE)
    if plain:
        return fix_mojibake(plain.group(1).strip())
    return ""


def download_file(url: str, account: dict, dest_dir: Path, origin_domain: str = "", filename: str = "") -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Only send cookies if the file URL is on the mirror's own origin (signed
    # CDN links reject credentials and would leak the session).
    headers = _file_headers(account, origin_domain or url.rsplit("/", 1)[0])
    file_host = urlparse(url).hostname or ""
    origin_host = urlparse(origin_domain).hostname if origin_domain else ""
    if origin_host and file_host == origin_host:
        headers.update({
            "Cookie": f"remix_userid={account['userid']}; remix_userkey={account['userkey']}",
        })
    req = urllib.request.Request(url, headers=headers, method="GET")
    last_error = None
    # Try direct first (bypasses the dead proxy that breaks file-CDN TLS), then proxy.
    for direct in (True, False):
        try:
            with _open_url(req, 120, direct) as resp:
                if not filename:
                    disp = resp.headers.get("Content-Disposition", "")
                    filename = header_filename(disp) or (
                        url.rsplit("/", 1)[-1].split("?")[0] or "book.bin"
                    )
                filename = fix_mojibake(filename)
                filename = re.sub(r'[\\/:*?"<>|]', "_", filename).strip() or "book.bin"
                dest = dest_dir / filename
                with open(dest, "wb") as out:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                return dest
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise last_error


def book_filename(book_name: str, book_ext: str = "") -> str:
    """Build the exact filename download_file() would use for a book."""
    base = re.sub(r'[\\/:*?"<>|]', "_", str(book_name or "")).strip()
    ext = str(book_ext or "").strip().lstrip(".")
    return base + ("." + ext if ext and base else "")


def load_download_log() -> dict[str, dict]:
    """Return {book_id: last entry} from the append-only download log."""
    result: dict[str, dict] = {}
    if not DOWNLOAD_LOG.exists():
        return result
    try:
        for line in DOWNLOAD_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            key = str(item.get("id") or "").strip()
            if key:
                result[key] = item
    except Exception:  # noqa: BLE001
        pass
    return result


def record_download(book_id: str, book_name: str, book_ext: str, dest: Path, email: str) -> None:
    entry = {
        "id": str(book_id or ""),
        "name": str(book_name or ""),
        "ext": str(book_ext or "").lstrip("."),
        "path": str(dest),
        "email": str(email or ""),
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    with DOWNLOAD_LOG.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def find_existing_download(book_id: str = "", book_name: str = "", book_ext: str = "") -> Path | None:
    """Check the local download log and downloads folder before spending quota.

    Resolving a download link costs one quota unit even if the transfer fails,
    so a repeat click must be caught *before* get_download_link() is called.
    Matching is by book id first (reliable), then by filename (for files that
    were downloaded before the log existed).
    """
    book_id = str(book_id or "").strip()
    if book_id:
        entry = load_download_log().get(book_id)
        if entry:
            path = Path(str(entry.get("path") or ""))
            if path.is_file():
                return path
    name = book_filename(book_name, book_ext)
    if not name:
        return None
    try:
        direct = DOWNLOAD_DIR / name
        if direct.is_file():
            return direct
        # Case-insensitive / stem match catches extension differences.
        stem = Path(name).stem.casefold()
        for path in DOWNLOAD_DIR.iterdir():
            if path.is_file() and path.stem.casefold() == stem:
                return path
    except OSError:  # noqa: BLE001
        pass
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"day": "", "cursor": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_list() -> int:
    accounts = parse_accounts()
    if not accounts:
        print("[!] No accounts. Add them to accounts.txt (email | userid | userkey).")
        return 1
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"Accounts ({len(accounts)}) - checking today's quota ({today})...")
    print("-" * 62)
    for index, account in enumerate(accounts, 1):
        try:
            q = get_quota(account)
            print(f"{index}. {q['email']:<32} {q['used']}/{q['limit']} used, {q['remaining']} left")
        except Exception as exc:  # noqa: BLE001
            print(f"{index}. {account['email']:<32} quota check failed: {exc}")
    return 0


def _save_account(email: str, userid: str, userkey: str) -> int:
    account = {"email": email, "userid": userid, "userkey": userkey}
    print("\nVerifying this account (read-only quota check)...")
    try:
        q = get_quota(account)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Verification failed: {exc}")
        print("    Cookie values may be wrong, or no domain is reachable right now.")
        print("    Nothing was saved.")
        return 1
    print(f"    OK: {q['email']} - {q['used']}/{q['limit']} used, {q['remaining']} left")

    existing = parse_accounts()
    if any(a["email"].lower() == q["email"].lower() for a in existing):
        print(f"[!] {q['email']} is already in accounts.txt. Nothing was added.")
        return 1

    with open(ACCOUNTS_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"{q['email']} | {userid} | {userkey}\n")
    print(f"[OK] Added to {ACCOUNTS_FILE}")
    return 0


def _add_by_login() -> int:
    """Log in with email+password, grab the cookie, then restore the old session."""
    if not ZLIB_EXE.exists():
        print(f"[!] Not found: {ZLIB_EXE}")
        return 1
    email = input("Email: ").strip()
    if not email:
        print("Cancelled.")
        return 1
    if sys.stdin.isatty():
        password = getpass.getpass("Password (hidden): ")
    else:
        password = input("Password (plain, no console): ")
    if not password:
        print("[!] Password is required.")
        return 1

    session = Path.home() / ".config" / "zlib" / "session.json"
    backup = session.read_bytes() if session.exists() else None
    try:
        domain = LAST_DOMAIN_FILE.read_text(encoding="utf-8").strip() or "https://z-lib.gd"
    except Exception:  # noqa: BLE001
        domain = "https://z-lib.gd"
    print(f"Logging in via {domain} ...")
    args = [str(ZLIB_EXE), "login", "--domain", domain,
            "--email", email, "--password", password]
    try:
        proc = subprocess.run(args, cwd=str(ROOT))
        if proc.returncode != 0:
            print("[!] Login failed. Nothing was added.")
            return 1
        data = json.loads(session.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or {}
        userid = cookies.get("remix_userid", "")
        userkey = cookies.get("remix_userkey", "")
        if not userid or not userkey:
            print("[!] Login succeeded but no auth cookie was saved. Nothing was added.")
            return 1
        return _save_account(email, userid, userkey)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Failed: {exc}")
        return 1
    finally:
        # Restore the original session so the current account/browser state is intact.
        try:
            if backup is None:
                session.unlink(missing_ok=True)
            else:
                session.write_bytes(backup)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Warning: could not restore session.json: {exc}")


def _add_by_cookie() -> int:
    print("Paste the two cookie values from browser (F12 > Application > Cookies).")
    email = input("Email: ").strip()
    if not email:
        print("Cancelled.")
        return 1
    userid = input("remix_userid: ").strip()
    userkey = input("remix_userkey: ").strip()
    if not userid or not userkey:
        print("[!] Both cookie values are required.")
        return 1
    return _save_account(email, userid, userkey)


def cmd_add() -> int:
    """Interactively add an account, verifying its quota before saving."""
    print("Add a Z-Library account")
    print("  1  Email + password (recommended - cookie fetched automatically)")
    print("  2  Paste remix_userid / remix_userkey from browser")
    choice = input("Select 1-2 [1]: ").strip() or "1"
    if choice == "2":
        return _add_by_cookie()
    return _add_by_login()


def cmd_download(book_refs: list[str]) -> int:
    accounts = parse_accounts()
    if not accounts:
        print("[!] No accounts. Add them to accounts.txt.")
        return 1
    if not book_refs:
        print("Usage: zlib_rotate.py download <id:hash> [id:hash ...]")
        return 2
    print("[i] Note: Z-Library counts 1 download as soon as the file link is")
    print("    resolved, even if the file transfer later fails.")

    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state = {"day": today, "cursor": 0}
    cursor = int(state.get("cursor", 0)) % len(accounts)

    failures = 0
    for book_ref in book_refs:
        print(f"\n=== {book_ref} ===")
        placed = False
        for step in range(len(accounts)):
            idx = (cursor + step) % len(accounts)
            account = accounts[idx]
            try:
                quota = get_quota(account)
                if quota["remaining"] is not None and quota["remaining"] <= 0:
                    print(f"  [{idx + 1}] {quota['email']}: quota exhausted ({quota['used']}/{quota['limit']}) - switching")
                    continue
                print(f"  [{idx + 1}] {quota['email']}: {quota['remaining']} left - using this account")
                link, domain = get_download_link(account, book_ref)
                print(f"  resolved via {domain}")
                dest = download_file(link, account, DOWNLOAD_DIR, origin_domain=domain)
                print(f"  OK saved: {dest}")
                cursor = idx  # keep using this account until it runs out
                placed = True
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    body = ""
                    try:
                        body = exc.read(2000).decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        pass
                    if ("IP Restricted" in body or "cloudflare" in body.lower()
                            or not body):
                        print("  [!] 403: file CDN (Cloudflare) blocks this network's IP.")
                        print("      IP/region restriction - not a login or code problem.")
                        print("      Try another network or a proxy/VPN with a different exit IP.")
                        failures += 1
                        break
                print(f"  [{idx + 1}] HTTP {exc.code} - trying next account")
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  [{idx + 1}] failed: {exc} - trying next account")
                continue
        if not placed:
            print("  [!] No account could download this book (all out of quota or failed).")
            failures += 1
        state["cursor"] = cursor
        save_state(state)
    return 1 if failures else 0


def cmd_browser(book_refs: list[str]) -> int:
    """Resolve a signed file link, then hand it to the system browser.

    Useful when the file CDN blocks this process's network path but the
    browser (possibly on a different proxy) can still reach it. Resolving a
    link still consumes one of the account's daily downloads.
    """
    accounts = parse_accounts()
    if not accounts:
        print("[!] No accounts. Add them to accounts.txt.")
        return 1
    if not book_refs:
        print("Usage: zlib_rotate.py browser <id:hash> [id:hash ...]")
        return 2
    print("[i] Resolving a link counts as 1 download even if the browser fails.")
    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state = {"day": today, "cursor": 0}
    cursor = int(state.get("cursor", 0)) % len(accounts)
    links: list[str] = []

    for book_ref in book_refs:
        print(f"\n=== {book_ref} ===")
        for step in range(len(accounts)):
            idx = (cursor + step) % len(accounts)
            account = accounts[idx]
            try:
                quota = get_quota(account)
                if quota["remaining"] is not None and quota["remaining"] <= 0:
                    print(f"  [{idx + 1}] quota exhausted - switching")
                    continue
                link, domain = get_download_link(account, book_ref)
                print(f"  resolved via {domain}; opening in browser")
                links.append(book_ref + "\t" + link)
                try:
                    webbrowser.open(link)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [!] could not launch browser: {exc}")
                cursor = idx
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  [{idx + 1}] failed: {exc} - trying next account")
                continue
        else:
            print("  [!] no account could resolve this book")

    if links:
        LINKS_FILE.write_text("\n".join(links) + "\n", encoding="utf-8")
        print(f"\nLinks saved to {LINKS_FILE} (paste into browser if needed).")
    state["cursor"] = cursor
    save_state(state)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: zlib_rotate.py [list|add|download|browser|folder <id:hash> ...]")
        return 2
    action = sys.argv[1].lower()
    if action == "add":
        return cmd_add()
    if action == "folder":
        print(DOWNLOAD_DIR)
        return 0
    if action == "list":
        return cmd_list()
    if action == "download":
        return cmd_download(sys.argv[2:])
    if action == "browser":
        return cmd_browser(sys.argv[2:])
    print(f"Unknown command: {action}")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
