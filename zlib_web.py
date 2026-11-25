#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local web UI for heartleo/zlib.

Start:  python zlib_web.py
Then open the URL printed in the console (browser opens automatically).
All search requests are executed locally by zlib.exe; nothing is uploaded.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

ROOT = Path(__file__).resolve().parent
ZLIB_EXE = ROOT / "zlib.exe"
HTML_FILE = ROOT / "zlib_web.html"
SESSION_FILE = Path.home() / ".config" / "zlib" / "session.json"
PORTS = range(18765, 18776)

try:
    import zlib_rotate as _rot

    DOWNLOAD_DIR = _rot.DOWNLOAD_DIR
except Exception:  # noqa: BLE001
    DOWNLOAD_DIR = ROOT / "downloads"

# Quota overview is read-only; cache it so the page does not hammer the EAPI.
_QUOTA_CACHE: dict[str, object] = {"at": 0.0, "items": []}
QUOTA_TTL = 90

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_zlib(args: list[str], timeout: int = 90) -> tuple[int, str, str]:
    if not ZLIB_EXE.exists():
        return 127, "", f"找不到 {ZLIB_EXE}"
    try:
        proc = subprocess.run(
            [str(ZLIB_EXE), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时：{timeout} 秒"
    except Exception as exc:  # noqa: BLE001
        return 126, "", str(exc)


SIMPLE_UA = "Mozilla/5.0"

# Z-Library mirrors churn constantly. These are the official domains tracked by
# zlib itself (internal/zlib/domain_policy.go). Auth cookies are account-level:
# the same remix_userid/remix_userkey work on any of them, so we keep one cookie
# and just find whichever domain is reachable right now.
KNOWN_DOMAINS = [
    "https://z-lib.gd",
    "https://z-lib.gl",
    "https://z-library.ec",
    "https://zlib.bz",
    "https://z-lib.by",
    "https://z-library.qa",
    "https://z-library.website",
    "https://1lib.sk",
    "https://article.sk",
    "https://articles.sk",
    "https://z-lib.fm",
    "https://z-lib.sk",
    "https://z-library.im",
    "https://z-library.sk",
]

COOKIE_FILE = ROOT / "cookie.txt"     # hand-editable: remix_userid=... / remix_userkey=...
DOMAIN_OVERRIDE = ROOT / "domain.txt"  # optional: pin one domain, one per file


def _http_get(url: str, cookies: dict[str, str], timeout: int) -> str:
    headers = {
        "Accept": "application/json",
        "User-Agent": SIMPLE_UA,
        "remix-userid": cookies.get("remix_userid", ""),
        "remix-userkey": cookies.get("remix_userkey", ""),
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def discover_domains(cookies: dict[str, str], timeout: int = 8) -> list[str]:
    """Ask Z-Library which official domains are live right now.

    Falls back to the built-in list if the info endpoint is unreachable.
    """
    found: list[str] = []
    for seed in KNOWN_DOMAINS[:4]:
        try:
            payload = json.loads(
                _http_get(seed + "/eapi/info/domains", cookies, timeout)
            )
            entries = payload.get("domains") or []
            for entry in entries:
                host = entry.get("domain") if isinstance(entry, dict) else entry
                if isinstance(host, str) and host.strip():
                    found.append("https://" + host.strip().rstrip("/"))
            if found:
                break
        except Exception:  # noqa: BLE001
            continue
    return found


def read_cookie_file() -> dict[str, str]:
    """Read the hand-editable cookie.txt (key=value per line, # = comment)."""
    cookies: dict[str, str] = {}
    if not COOKIE_FILE.exists():
        return cookies
    try:
        for line in COOKIE_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lower().lstrip("-")
            value = value.strip().strip("\"'")
            if key and value and not value.startswith("PASTE_"):
                cookies[key] = value
    except Exception:  # noqa: BLE001
        pass
    return cookies


def session_domain_and_cookies() -> tuple[str | None, dict[str, str], str | None]:
    # cookie.txt wins: the user can hand-fill it with no login or import.
    cookies = read_cookie_file()

    domain = ""
    if SESSION_FILE.exists():
        try:
            session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            domain = session.get("domain") or ""
            if not cookies:
                cookies = session.get("cookies") or {}
        except Exception:  # noqa: BLE001
            pass

    if not cookies.get("remix_userid") or not cookies.get("remix_userkey"):
        return None, {}, "未找到 Cookie：请在 cookie.txt 填入 remix_userid 和 remix_userkey"

    env_domain = os.environ.get("ZLIB_DOMAIN", "").strip()
    if not env_domain:
        env_file = Path.home() / ".config" / "zlib" / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() == "ZLIB_DOMAIN":
                        env_domain = value.strip().strip("\"'")
            except Exception:  # noqa: BLE001
                pass
    if env_domain:
        domain = env_domain

    # Domain is optional now: with auto-follow, an empty one just means we skip
    # the session hint and probe the official domains.
    if domain and not domain.startswith("http"):
        domain = ""
    return (domain.rstrip("/") or None), cookies, None


def _search_once(
    domain: str, cookies: dict[str, str], data: bytes
) -> tuple[bool, object]:
    """Try one domain. Returns (ok, payload_or_error)."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "source": "android",
        "User-Agent": SIMPLE_UA,
        "remix-userid": cookies.get("remix_userid", ""),
        "remix-userkey": cookies.get("remix_userkey", ""),
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    request = urllib.request.Request(
        domain + "/eapi/book/search", data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        try:
            detail = json.loads(body).get("error") or body
        except Exception:  # noqa: BLE001
            detail = body
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, payload


def _format_books(payload: dict, domain: str, page: int) -> dict:
    books = []
    for item in payload.get("books", []) or []:
        book_id = str(item.get("id") or "")
        book_hash = item.get("hash") or ""
        author = item.get("author")
        authors = author if isinstance(author, list) else ([author] if author else [])
        size = item.get("filesizeString") or str(item.get("filesize") or "")
        books.append(
            {
                "id": f"{book_id}:{book_hash}" if book_id and book_hash else book_id,
                "hash": book_hash,
                "name": item.get("title") or "",
                "authors": authors,
                "year": str(item.get("year") or ""),
                "publisher": item.get("publisher") or "",
                "language": item.get("language") or "",
                "extension": item.get("extension") or "",
                "size": size,
                "rating": str(item.get("interestScore") or ""),
                "cover": item.get("cover") or "",
                "url": urljoin(domain + "/", item.get("href") or ""),
            }
        )
    pagination = payload.get("pagination") or {}
    return {
        "books": books,
        "page": pagination.get("current") or page,
        "total_pages": pagination.get("total_pages") or 0,
        "domain": domain,
    }


def local_search(query: str, count: int, page: int, exts: list[str]) -> tuple[bool, object]:
    """Search via EAPI with one account cookie, trying domains until one works.

    Domains change frequently; the cookie does not. We cache the last working
    domain so normal searches hit it first and stay fast.
    """
    session_domain, cookies, error = session_domain_and_cookies()
    if error:
        return False, error

    pairs = [("message", query), ("page", str(page)), ("limit", str(count))]
    for ext in exts:
        pairs.append(("extensions[]", ext.lower()))
    data = urlencode(pairs).encode("utf-8")

    # Fast path: cached working domain first, then session/pinned/known domains.
    # Only ask Z-Library for fresh domains if none of these respond.
    domains: list[str] = []

    def add(domain: str | None) -> None:
        if not domain:
            return
        domain = domain.strip().rstrip("/")
        if domain.startswith("http") and domain not in domains:
            domains.append(domain)

    add(_last_domain())
    add(session_domain)
    add(os.environ.get("ZLIB_DOMAIN", ""))
    if DOMAIN_OVERRIDE.exists():
        try:
            for line in DOMAIN_OVERRIDE.read_text(encoding="utf-8-sig").splitlines():
                add(line)
        except Exception:  # noqa: BLE001
            pass
    for domain in KNOWN_DOMAINS:
        add(domain)

    last_error = "no reachable domain"
    tried: set[str] = set()

    def try_domains(candidates: list[str]) -> tuple[str, dict] | None:
        for domain in candidates:
            if domain in tried:
                continue
            tried.add(domain)
            ok, payload = _search_once(domain, cookies, data)
            if not ok:
                last_error = f"{domain}: {payload}"
                continue
            if not isinstance(payload, dict) or payload.get("success") != 1:
                last_error = f"{domain}: {(payload.get('error') if isinstance(payload, dict) else None) or 'search rejected'}"
                continue
            _remember_domain(domain)
            return domain, payload
        return None

    # Fast path: cached working domain first, then session/pinned/known domains.
    result = try_domains(domains)
    if result is None:
        # Slow path: ask Z-Library which domains are live, then retry those.
        fresh = discover_domains(cookies)
        result = try_domains(fresh)

    if result is None:
        return False, last_error
    domain, payload = result
    return True, _format_books(payload, domain, page)


LAST_DOMAIN_FILE = ROOT / ".last_domain"


def _last_domain() -> str | None:
    try:
        return LAST_DOMAIN_FILE.read_text(encoding="utf-8").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _remember_domain(domain: str) -> None:
    try:
        LAST_DOMAIN_FILE.write_text(domain, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def account_quotas(force: bool = False) -> dict:
    """Read-only quota snapshot of every account in accounts.txt."""
    import time

    now = time.time()
    if (
        not force
        and _QUOTA_CACHE["items"]
        and now - float(str(_QUOTA_CACHE["at"])) < QUOTA_TTL
    ):
        return {
            "ok": True,
            "accounts": _QUOTA_CACHE["items"],
            "cached": True,
            "age": int(now - float(str(_QUOTA_CACHE["at"]))),
        }
    try:
        import zlib_rotate as rot
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"账号模块加载失败：{exc}", "accounts": []}

    accounts = rot.parse_accounts()

    def read_one(account: dict) -> dict:
        item = {"email": account.get("email", ""), "ok": False}
        try:
            quota = rot.get_quota(account)
            item.update({
                "ok": True,
                "email": quota.get("email") or item["email"],
                "used": quota.get("used"),
                "limit": quota.get("limit"),
                "remaining": quota.get("remaining"),
            })
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)[:160]
        return item

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(accounts)))) as pool:
        items = list(pool.map(read_one, accounts))
    _QUOTA_CACHE["at"] = now
    _QUOTA_CACHE["items"] = items
    return {"ok": True, "accounts": items, "cached": False, "age": 0}


def invalidate_quota_cache() -> None:
    _QUOTA_CACHE["at"] = 0.0
    _QUOTA_CACHE["items"] = []


def send_json(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


# Cover images live on a CDN that rejects TLS 1.3 from this machine and the
# browser cannot load them directly. Fetch them locally with TLS 1.2 forced.
_COVER_CACHE: dict[str, tuple[bytes, str]] = {}


def fetch_cover(url: str) -> tuple[bytes, str] | None:
    if url in _COVER_CACHE:
        return _COVER_CACHE[url]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "cdn-zlib.sk" or host.endswith(".cdn-zlib.sk")):
        return None
    if len(url) > 2000:
        return None
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)
    )
    data, ctype = b"", ""
    for _attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=15) as resp:
                data = resp.read(2_000_000)
                ctype = resp.headers.get("Content-Type") or "image/jpeg"
            if data and ctype.startswith("image/"):
                break
        except Exception:  # noqa: BLE001
            continue
    if not data or not ctype.startswith("image/"):
        return None
    _COVER_CACHE[url] = (data, ctype)
    return data, ctype


class Handler(BaseHTTPRequestHandler):
    server_version = "zlib-web/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.serve_html()
        elif path == "/api/status":
            self.api_status()
        elif path == "/api/cover":
            self.api_cover()
        elif path == "/api/quotas":
            query = parse_qs(urlparse(self.path).query)
            send_json(self, account_quotas(force="refresh" in query))
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            send_json(self, {"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/domains":
            self.api_domains()
            return
        if path == "/api/download":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") or "{}"
                payload = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                send_json(self, {"ok": False, "error": f"无效请求：{exc}"}, 400)
                return
            self.api_download(payload)
            return
        if path != "/api/search":
            send_json(self, {"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") or "{}"
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            send_json(self, {"error": f"无效请求：{exc}"}, 400)
            return
        self.api_search(payload)

    def serve_html(self) -> None:
        if not HTML_FILE.exists():
            body = "Missing zlib_web.html".encode("utf-8")
            self.send_response(500)
        else:
            body = HTML_FILE.read_bytes()
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def api_cover(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        url = (query.get("url") or [""])[0]
        result = fetch_cover(url)
        if result is None:
            body = b""
            self.send_response(404)
        else:
            body, ctype = result
            self.send_response(200)
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def api_status(self) -> None:
        _, out, err = run_zlib(["version"], timeout=15)
        version = out.strip() or err.strip() or "unknown"
        cookies_ready = bool(
            read_cookie_file().get("remix_userid") and read_cookie_file().get("remix_userkey")
        ) or SESSION_FILE.exists()
        session_exists = cookies_ready
        profile = None
        profile_error = None
        if session_exists:
            config_file = Path.home() / ".config" / "zlib" / "config.json"
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
                profile = {"email": config.get("last_login_email")}
            except Exception:  # noqa: BLE001
                profile = {"email": None}
        send_json(
            self,
            {
                "version": version,
                "exe": str(ZLIB_EXE),
                "session_exists": session_exists,
                "session_file": str(COOKIE_FILE) if read_cookie_file() else str(SESSION_FILE),
                "cookie_source": "cookie.txt" if read_cookie_file() else "session.json",
                "profile": profile,
                "profile_error": profile_error,
                "download_dir": str(DOWNLOAD_DIR),
                "active_domain": _last_domain(),
                "domain_pinned": DOMAIN_OVERRIDE.exists(),
            },
        )

    def api_domains(self) -> None:
        _, cookies, error = session_domain_and_cookies()
        if error:
            send_json(self, {"error": error}, 400)
            return
        discovered = discover_domains(cookies)
        send_json(
            self,
            {
                "active": _last_domain(),
                "discovered": discovered,
                "known": KNOWN_DOMAINS,
                "pinned": DOMAIN_OVERRIDE.read_text(encoding="utf-8-sig").strip()
                if DOMAIN_OVERRIDE.exists()
                else None,
            },
        )

    def api_search(self, payload: dict) -> None:
        queries = payload.get("queries") or []
        if isinstance(queries, str):
            queries = [queries]
        queries = [str(q).strip() for q in queries if str(q).strip()]
        if not queries:
            send_json(self, {"error": "请至少输入一个书名"}, 400)
            return

        try:
            count = max(1, min(100, int(payload.get("count", 5))))
        except Exception:  # noqa: BLE001
            count = 5
        try:
            page = max(1, int(payload.get("page", 1)))
        except Exception:  # noqa: BLE001
            page = 1

        ext_raw = str(payload.get("ext", "") or "").strip()
        exts = [e.strip().lower() for e in ext_raw.replace("，", ",").split(",") if e.strip()]

        try:
            import zlib_rotate as rot

            existing_ids = set(rot.load_download_log().keys())
        except Exception:  # noqa: BLE001
            rot = None
            existing_ids = set()

        results = []
        for query in queries:
            item = {
                "query": query,
                "ok": False,
                "books": [],
                "error": None,
                "page": page,
                "total_pages": 0,
            }
            ok, data = local_search(query, count, page, exts)
            if ok and isinstance(data, dict):
                books = data.get("books", [])
                for b in books:
                    bid = str(b.get("id") or "")
                    existing = None
                    if rot and bid:
                        existing = rot.find_existing_download(
                            bid, b.get("name", ""), b.get("extension", "")
                        )
                    b["downloaded"] = bool(existing)
                    b["downloaded_path"] = str(existing) if existing else None
                item["books"] = books
                item["page"] = data.get("page", page)
                item["total_pages"] = data.get("total_pages", 0)
                item["ok"] = True
            else:
                item["error"] = str(data)
            results.append(item)

        send_json(self, {"results": results, "count": count, "page": page, "exts": exts})

    def api_download(self, payload: dict) -> None:
        """Download one book using the same multi-account rotation as the BAT."""
        book_ref = str(payload.get("id", "") or "").strip()
        mode = str(payload.get("mode", "direct") or "direct").strip().lower()
        if mode not in ("direct", "browser"):
            mode = "direct"
        book_name = str(payload.get("name", "") or "").strip()
        book_ext = str(payload.get("ext", "") or "").strip().lstrip(".")
        if not book_ref:
            send_json(self, {"ok": False, "error": "缺少书籍 ID"}, 400)
            return

        try:
            import zlib_rotate as rot
        except Exception as exc:  # noqa: BLE001
            send_json(self, {"ok": False, "error": f"下载模块加载失败：{exc}"}, 500)
            return

        # Duplicate protection runs BEFORE resolving a link: resolving already
        # costs one quota unit, so we must not get that far for a repeat click.
        existing = rot.find_existing_download(book_ref, book_name, book_ext)
        if existing:
            send_json(self, {
                "ok": True,
                "already": True,
                "mode": mode,
                "path": str(existing),
                "message": "下载目录里已有这本书，跳过（未消耗配额）",
            })
            return

        accounts = rot.parse_accounts()
        if not accounts:
            send_json(self, {"ok": False, "error": "accounts.txt 里还没有账号"}, 400)
            return

        state = rot.load_state()
        today = rot.datetime.now().strftime("%Y-%m-%d")
        if state.get("day") != today:
            state = {"day": today, "cursor": 0}
        cursor = int(state.get("cursor", 0)) % len(accounts)
        last_error = "未知错误"

        for step in range(len(accounts)):
            idx = (cursor + step) % len(accounts)
            account = accounts[idx]
            try:
                quota = rot.get_quota(account)
                if quota.get("remaining") is not None and quota["remaining"] <= 0:
                    last_error = f"{quota.get('email')} 今日配额已用完"
                    continue
                link, domain = rot.get_download_link(account, book_ref)
                cursor = idx
                state["cursor"] = cursor
                rot.save_state(state)
                invalidate_quota_cache()  # link resolution just spent quota
                if mode == "browser":
                    rot.LINKS_FILE.write_text(book_ref + "\t" + link + "\n", encoding="utf-8")
                    send_json(self, {
                        "ok": True,
                        "mode": "browser",
                        "link": link,
                        "account": quota.get("email"),
                        "domain": domain,
                    })
                    return
                # Prefer the correct title from search results over the
                # server's Content-Disposition name, which is often mojibake.
                preferred = ""
                if book_name:
                    base = re.sub(r'[\\/:*?"<>|]', "_", book_name).strip()
                    preferred = base + ("." + book_ext if book_ext else "")
                dest = rot.download_file(
                    link, account, rot.DOWNLOAD_DIR,
                    origin_domain=domain, filename=preferred,
                )
                rot.record_download(book_ref, book_name, book_ext, dest, quota.get("email", ""))
                send_json(self, {
                    "ok": True,
                    "mode": "direct",
                    "path": str(dest),
                    "account": quota.get("email"),
                    "domain": domain,
                })
                return
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read(2000).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    pass
                if exc.code == 403:
                    last_error = (
                        "文件 CDN 的 Cloudflare 按出口 IP 封锁了当前网络"
                        "（Edge IP Restricted）。换网络/VPN 节点，或点「浏览器」按钮试试。"
                    )
                    break
                last_error = f"HTTP {exc.code}: {body[:200]}"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        send_json(self, {"ok": False, "error": last_error})


def pick_port() -> tuple[ThreadingHTTPServer, int]:
    for port in PORTS:
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler), port
        except OSError:
            continue
    raise RuntimeError(f"端口 {PORTS.start}-{PORTS.stop - 1} 都被占用")


def selftest() -> None:
    import urllib.request

    server, port = pick_port()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    try:
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8", "replace")
            print("index:", resp.status, "bytes=", len(html.encode("utf-8")))
        with urllib.request.urlopen(base + "/api/status", timeout=30) as resp:
            print("status:", resp.status, resp.read().decode("utf-8", "replace")[:400])
        req = urllib.request.Request(
            base + "/api/search",
            data=json.dumps({"queries": ["马斯克的钢铁侠"], "count": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            print("search:", resp.status, resp.read().decode("utf-8", "replace")[:400])
        # Route check only: empty id must be rejected before any network call.
        dl_req = urllib.request.Request(
            base + "/api/download",
            data=b'{"id":"","mode":"direct"}',
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(dl_req, timeout=10)
        except urllib.error.HTTPError as exc:
            print("download route:", exc.code, exc.read().decode("utf-8", "replace")[:120])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_query_file(path: Path, count: int) -> int:
    """Search every non-empty line in a UTF-8 query file (batch mode)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot read query file: {exc}", file=sys.stderr)
        return 2
    queries = [line.strip() for line in text.splitlines() if line.strip()]
    if not queries:
        print("Query file is empty.", file=sys.stderr)
        return 2

    last_code = 0
    for query in queries:
        print(f"--- {query} ---")
        ok, data = local_search(query, count, 1, [])
        if not ok:
            print(f"Error: {data}", file=sys.stderr)
            last_code = 1
            continue
        books = data.get("books", []) if isinstance(data, dict) else []
        if not books:
            print("No results found.")
            continue
        for index, book in enumerate(books, 1):
            authors = ", ".join(book.get("authors") or [])
            print(
                f"{index}. {book.get('name', '')} | {authors} | "
                f"{book.get('year', '')} | {book.get('extension', '')} | "
                f"{book.get('size', '')} | ID: {book.get('id', '')}"
            )
    return last_code


def main() -> None:
    if "--search-file" in sys.argv:
        idx = sys.argv.index("--search-file")
        if idx + 1 >= len(sys.argv):
            print("Usage: zlib_web.py --search-file <file> [--count N]", file=sys.stderr)
            raise SystemExit(2)
        query_file = Path(sys.argv[idx + 1])
        try:
            count = int(sys.argv[sys.argv.index("--count") + 1]) if "--count" in sys.argv else 5
        except Exception:  # noqa: BLE001
            count = 5
        raise SystemExit(run_query_file(query_file, count))

    if "--selftest" in sys.argv:
        selftest()
        return

    server, port = pick_port()
    url = f"http://127.0.0.1:{port}/"
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        print(f"zlib 网页已启动：{url}")
        print("浏览器会自动打开；关闭此窗口即可停止服务。")
    else:
        print(f"zlib 网页已启动：{url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
