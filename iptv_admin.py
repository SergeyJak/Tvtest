#!/usr/bin/env python3
"""Read-only HeySmart TV administration, isolated from the streaming gateway.

Binds to loopback only. Publish /admin/ through an existing HTTPS reverse proxy.
A separate random password (not the TV playlist token) protects all data APIs.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from iptv_multi import channels_from_text
from iptv_telemetry import query_viewing, query_incidents

ASSETS = Path(__file__).resolve().parent / "admin"
SESSION_SECONDS = 8 * 3600
COOKIE_NAME = "heysmart_tv_admin"
MAX_LOGIN_BYTES = 2048


def read_or_create_password(path: Path) -> str:
    """Create a dedicated, long random credential with owner-only permissions."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(secrets.token_urlsafe(32) + "\n")
    if path.is_symlink():
        raise ValueError("Password file cannot be a symlink")
    mode = path.stat().st_mode
    if mode & 0o077:
        raise ValueError("Password file must be readable only by its owner (chmod 600)")
    password = path.read_text(encoding="ascii").strip()
    if len(password) < 24:
        raise ValueError("Admin password must be at least 24 characters")
    return password


def catalogue(playlist_file: Path) -> list[dict]:
    """Read labels and M3U group-title while retaining the gateway's stable IDs.

    The gateway currently ignores group-title when calculating IDs. Changing
    that would break saved TV/EPG links, so the admin only uses it for display.
    """
    from urllib.parse import urlsplit

    raw = playlist_file.read_text(encoding="utf-8-sig")
    entries = channels_from_text(raw)
    display_groups: list[str] = []
    pending = False
    group = ""
    for source_line in raw.splitlines():
        line = source_line.strip()
        if line.startswith("#EXTINF:"):
            pending = True
            match = re.search(r'group-title\s*=\s*"([^"]*)"', line, flags=re.I)
            group = match.group(1) if match else ""
        elif line.startswith("#EXTGRP:") and pending:
            group = line[len("#EXTGRP:"):].strip()
        elif line and not line.startswith("#") and pending:
            if urlsplit(line).scheme in ("http", "https"):
                display_groups.append(group)
            pending = False
    return [
        {"id": c.id, "name": c.name, "group": display_groups[index] or c.group}
        for index, c in enumerate(entries)
    ]


def scan_epg(path: Path) -> dict:
    """Streaming scan, bounded memory even for 43 MB compressed XMLTV."""
    ids: set[str] = set()
    count = 0
    with gzip.open(path, "rb") as source:
        iterator = ET.iterparse(source, events=("start", "end"))
        root = None
        for event, node in iterator:
            if root is None and event == "start":
                if node.tag != "tv":
                    raise ValueError("Invalid XMLTV root")
                root = node
            if event == "end":
                if node.tag == "programme":
                    if node.get("channel"):
                        ids.add(node.get("channel"))
                    count += 1
                node.clear()
                if root is not None and node.tag in ("channel", "programme"):
                    root.clear()
    return {"covered_ids": ids, "programmes": count}


class EpgCache:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.key: tuple[int, int] | None = None
        self.loading = False
        self.data: dict = {"status": "missing", "covered_ids": set(), "programmes": 0}

    def snapshot(self) -> dict:
        try:
            stat = self.path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            updated = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            size = stat.st_size
        except OSError:
            with self.lock:
                self.key = None
                self.data = {"status": "missing", "covered_ids": set(), "programmes": 0}
                return dict(self.data, updated_at=None, size_bytes=0)
        with self.lock:
            if key != self.key and not self.loading:
                self.loading = True
                self.key = key
                self.data = {"status": "loading", "covered_ids": set(), "programmes": 0}
                threading.Thread(target=self._load, args=(key,), daemon=True).start()
            return dict(self.data, updated_at=updated, size_bytes=size)

    def _load(self, key: tuple[int, int]) -> None:
        try:
            result = scan_epg(self.path)
            data = {"status": "ready", **result}
        except (OSError, EOFError, ET.ParseError, ValueError):
            data = {"status": "error", "covered_ids": set(), "programmes": 0}
        with self.lock:
            if key == self.key:
                self.data = data
            self.loading = False


class AdminState:
    def __init__(self, playlist: Path, epg: Path, gateway_port: int,
                 gateway_token_file: Path, password: str,
                 telemetry_db: Path | None = None):
        self.playlist = playlist
        self.epg = EpgCache(epg)
        self.gateway_port = gateway_port
        self.gateway_token_file = gateway_token_file
        self.password = password
        self.telemetry_db = Path(telemetry_db or ".iptv_state/telemetry.sqlite3")
        self.sessions: dict[str, float] = {}
        self.failed: dict[str, list[float]] = defaultdict(list)
        self.lock = threading.Lock()
        self.started = time.time()
        self._catalogue_key = None
        self._channels: list[dict] = []

    def channels(self) -> list[dict]:
        try:
            stat = self.playlist.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return []
        with self.lock:
            if key != self._catalogue_key:
                self._channels = catalogue(self.playlist)
                self._catalogue_key = key
            return [dict(c) for c in self._channels]

    def gateway_status(self) -> dict | None:
        try:
            token = self.gateway_token_file.read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{12,}", token):
                return None
            url = f"http://127.0.0.1:{self.gateway_port}/{token}/status"
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status != 200:
                    return None
                return json.loads(response.read(2 * 1024 * 1024))
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def login(self, password: str, peer: str) -> str | None:
        now = time.time()
        with self.lock:
            attempts = [t for t in self.failed[peer] if now - t < 600]
            self.failed[peer] = attempts
            if len(attempts) >= 10:
                return None
            if not hmac.compare_digest(password, self.password):
                self.failed[peer].append(now)
                return None
            self.failed.pop(peer, None)
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = now + SESSION_SECONDS
            if len(self.sessions) > 256:
                self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            return token

    def authenticated(self, value: str) -> bool:
        if not value:
            return False
        digest = hashlib.sha256(value.encode()).hexdigest()
        now = time.time()
        with self.lock:
            expiry = self.sessions.get(digest, 0)
            if expiry < now:
                self.sessions.pop(digest, None)
                return False
            return True

    def logout(self, value: str):
        if value:
            with self.lock:
                self.sessions.pop(hashlib.sha256(value.encode()).hexdigest(), None)

    def overview(self) -> dict:
        guide = self.epg.snapshot()
        channels = self.channels()
        status = self.gateway_status()
        covered = guide["covered_ids"]
        active = []
        if isinstance(status, dict):
            for stream in status.get("streams", []):
                active.append({
                    "id": stream.get("id"),
                    "name": stream.get("channel"),
                    "ready": bool(stream.get("ready")),
                    "pinned": bool(stream.get("pinned")),
                    "stalled": bool(stream.get("upstream_stalled")),
                    "buffer_state": stream.get("buffer_state"),
                    "cached_mib": stream.get("cached_mib"),
                    "delay_seconds": stream.get("effective_buffer_delay_seconds"),
                    "segment_404": stream.get("client_segment_404"),
                    "manifest_503": stream.get("client_manifest_503"),
                    "seconds_since_access": stream.get("seconds_since_access"),
                })
        return {
            "gateway_online": status is not None,
            "channels": len(channels),
            "active_streams": len(active),
            "max_streams": status.get("max_streams") if status else None,
            "delay_seconds": status.get("delay_seconds") if status else None,
            "stream_limit_denials": status.get("stream_limit_denials") if status else None,
            "warmup_timeouts": status.get("warmup_timeouts") if status else None,
            "catalog_requests": status.get("catalog_requests") if status else None,
            "admin_uptime_seconds": int(time.time() - self.started),
            "streams": active,
            "epg": {
                "status": guide["status"],
                "covered": len(covered) if guide["status"] == "ready" else None,
                "programmes": guide["programmes"] if guide["status"] == "ready" else None,
                "size_bytes": guide["size_bytes"],
                "updated_at": guide["updated_at"],
            },
        }

    def _channel_labels(self) -> dict:
        return {row["id"]: row["name"] for row in self.channels()}

    def viewing_history(self, days: int = 7) -> dict:
        data = query_viewing(self.telemetry_db, days)
        names = self._channel_labels()
        for row in data["channels"] + data["sessions"]:
            row["channel_name"] = names.get(row["channel_id"], "Неизвестный канал")
        state = self.gateway_status()
        data["recording_active"] = bool(state and state.get("telemetry_enabled"))
        return data

    def incident_history(self, days: int = 7, kind: str = "all") -> dict:
        data = query_incidents(self.telemetry_db, days, kind)
        names = self._channel_labels()
        for row in data["events"]:
            row["channel_name"] = names.get(row["channel_id"], "Неизвестный канал")
        state = self.gateway_status()
        data["recording_active"] = bool(state and state.get("telemetry_enabled"))
        return data

    def channel_response(self) -> dict:
        guide = self.epg.snapshot()
        active_status = self.gateway_status()
        active = {s["id"] for s in active_status.get("streams", [])} if active_status else set()
        covered = guide["covered_ids"]
        return {"epg_status": guide["status"], "channels": [
            dict(c, epg=(c["id"] in covered) if guide["status"] == "ready" else None,
                 active=c["id"] in active)
            for c in self.channels()
        ]}


def make_admin_handler(state: AdminState, *, secure_cookies: bool = True):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # Never log passwords, cookies, TV playlist keys or client addresses.
            pass

        def do_HEAD(self):
            self.route(head=True)

        def do_GET(self):
            self.route(head=False)

        def do_POST(self):
            self.route(head=False)

        def cookie(self) -> str:
            try:
                parsed = http.cookies.SimpleCookie()
                parsed.load(self.headers.get("Cookie", ""))
                return parsed[COOKIE_NAME].value if COOKIE_NAME in parsed else ""
            except http.cookies.CookieError:
                return ""

        def respond(self, code, body=b"", mime="application/json", *, head=False,
                    headers=None):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self' data:; "
                             "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def cookie_header(self, session: str, *, clear=False) -> str:
            value = "" if clear else session
            parts = [f"{COOKIE_NAME}={value}", "Path=/admin",
                     "HttpOnly", "SameSite=Strict",
                     f"Max-Age={0 if clear else SESSION_SECONDS}"]
            if secure_cookies:
                parts.append("Secure")
            return "; ".join(parts)

        def route(self, *, head: bool):
            from urllib.parse import urlsplit
            path = urlsplit(self.path).path
            if path == "/admin":
                return self.respond(308, b"", "text/plain", head=head,
                                    headers={"Location": "/admin/"})
            if path not in ("/admin/", "/admin/app.js", "/admin/style.css",
                            "/admin/api/session", "/admin/api/login",
                            "/admin/api/logout", "/admin/api/overview",
                            "/admin/api/channels", "/admin/api/history",
                            "/admin/api/incidents"):
                return self.respond(404, b"Not found", "text/plain", head=head)
            if path in ("/admin/", "/admin/app.js", "/admin/style.css"):
                if self.command not in ("GET", "HEAD"):
                    return self.respond(405, b"", head=head)
                name = {
                    "/admin/": "index.html", "/admin/app.js": "app.js",
                    "/admin/style.css": "style.css"
                }
                file = ASSETS / name[path]
                try:
                    data = file.read_bytes()
                except OSError:
                    return self.respond(503, b"UI unavailable", "text/plain", head=head)
                mime = ("text/html; charset=utf-8" if path == "/admin/" else
                        "text/javascript; charset=utf-8" if path.endswith(".js")
                        else "text/css; charset=utf-8")
                return self.respond(200, data, mime, head=head)
            if path == "/admin/api/login" and self.command == "POST":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_LOGIN_BYTES:
                        raise ValueError("Invalid body size")
                    payload = json.loads(self.rfile.read(size))
                    password = payload.get("password")
                    if not isinstance(password, str):
                        raise ValueError("Invalid password")
                except (ValueError, json.JSONDecodeError):
                    return self.respond(400, b'{"error":"invalid_request"}', head=head)
                token = state.login(password, self.client_address[0])
                if not token:
                    return self.respond(401, b'{"error":"invalid_credentials"}', head=head)
                return self.respond(200, b'{"ok":true}', head=head,
                                    headers={"Set-Cookie": self.cookie_header(token)})
            current = self.cookie()
            if not state.authenticated(current):
                return self.respond(401, b'{"error":"unauthorized"}', head=head)
            if path == "/admin/api/logout" and self.command == "POST":
                state.logout(current)
                return self.respond(200, b'{"ok":true}', head=head,
                                    headers={"Set-Cookie": self.cookie_header("", clear=True)})
            if self.command not in ("GET", "HEAD"):
                return self.respond(405, b"", head=head)
            if path == "/admin/api/session":
                return self.respond(200, b'{"ok":true}', head=head)
            if path in ("/admin/api/history", "/admin/api/incidents"):
                from urllib.parse import parse_qs
                qs = parse_qs(urlsplit(self.path).query)
                try:
                    days = int(qs.get("days", ["7"])[0])
                    if days not in (1, 7, 30):
                        raise ValueError()
                    kind = qs.get("kind", ["all"])[0]
                    data = (state.viewing_history(days) if path.endswith("/history")
                            else state.incident_history(days, kind))
                except (ValueError, TypeError):
                    return self.respond(400, b'{"error":"invalid_filter"}', head=head)
                return self.respond(200, json.dumps(data, ensure_ascii=False,
                                                    separators=(",", ":")).encode("utf-8"), head=head)
            if path == "/admin/api/overview":
                data = state.overview()
            elif path == "/admin/api/channels":
                data = state.channel_response()
            else:
                return self.respond(404, b"Not found", "text/plain", head=head)
            return self.respond(200, json.dumps(data, ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8"),
                                head=head)
    return Handler


def main():
    p = argparse.ArgumentParser(description="Private read-only TV administration")
    p.add_argument("--playlist", type=Path, default=Path("forgpt.m3u8"))
    p.add_argument("--epg-file", type=Path, default=Path("epg.xml.gz"))
    p.add_argument("--token-file", type=Path, default=Path(".iptv_gateway_token"))
    p.add_argument("--telemetry-db", type=Path, default=Path(".iptv_state/telemetry.sqlite3"))
    p.add_argument("--password-file", type=Path, default=Path(".iptv_admin_password"))
    p.add_argument("--gateway-port", type=int, default=8787)
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--local-http", action="store_true",
                   help="Allow local HTTP cookies (SSH tunnel testing only)")
    args = p.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        p.error("Admin server must bind to loopback; use a TLS reverse proxy")
    if not 1 <= args.port <= 65535 or not 1 <= args.gateway_port <= 65535:
        p.error("Invalid port")
    password = read_or_create_password(args.password_file)
    state = AdminState(args.playlist, args.epg_file, args.gateway_port,
                       args.token_file, password, args.telemetry_db)
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_admin_handler(state,
                                                    secure_cookies=not args.local_http))
    server.daemon_threads = True
    print(f"HeySmart TV Admin ready on loopback port {args.port}; "
          "password stored privately in --password-file.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
