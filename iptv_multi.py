#!/usr/bin/env python3
"""LAN-only on-demand multi-channel HLS gateway.

The channel catalog does not expose upstream URLs. One worker/cache is started
per active channel and stopped when idle. This is a testing mode, not an
internet-facing family streaming server.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from iptv_gateway import (
    Gateway, load_or_create_token, private_playlist_text, safe_error,
)
from iptv_healthcheck import entries_from_m3u


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    group: str
    url: str


def channels_from_entries(entries: list[tuple[str, str, str]]) -> list[Channel]:
    """Stable public IDs depend on channel identity, not a private URL."""
    seen: dict[tuple[str, str], int] = {}
    channels = []
    for name, group, url in entries:
        if not name or urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            continue
        identity = (name.casefold(), group.casefold())
        ordinal = seen.get(identity, 0)
        seen[identity] = ordinal + 1
        marker = f"{identity[0]}\x00{identity[1]}\x00{ordinal}".encode("utf-8")
        cid = hashlib.sha256(marker).hexdigest()[:20]
        channels.append(Channel(cid, name, group, url))
    return channels


def channels_from_text(text: str) -> list[Channel]:
    """Private playlist parsing without exposing its URLs to a TV."""
    # Matches the small #EXTINF / #EXTGRP subset of entries_from_m3u.
    name = None
    group = ""
    result = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            name = line.rsplit(",", 1)[-1].strip()
            group = ""
        elif line.startswith("#EXTGRP:"):
            group = line[len("#EXTGRP:"):].strip()
        elif line and not line.startswith("#") and name is not None:
            result.append((name, group, line))
            name = None
    return channels_from_entries(result)


class ActiveStream:
    def __init__(self, channel: Channel, gateway: Gateway, slots: threading.BoundedSemaphore,
                 pinned: bool = False):
        self.channel = channel
        self.gateway = gateway
        self.pinned = pinned
        self.accessed = time.monotonic()
        self.pending_requests = 0
        self.worker = threading.Thread(target=self._run, args=(slots,), daemon=True)
        self.worker.start()

    def _run(self, slots: threading.BoundedSemaphore):
        # A replaced worker may still be reading from the provider.
        while not self.gateway.stop.is_set():
            if slots.acquire(timeout=0.25):
                try:
                    if not self.gateway.stop.is_set():
                        self.gateway.run()
                finally:
                    slots.release()
                return

    def close(self):
        self.gateway.stop.set()


class MultiGateway:
    def __init__(self, channels: list[Channel], playlist_path: Path,
                 private_url: str | None = None, delay: int = 30,
                 timeout: float = 12, poll: float = 2, cache_mib: int = 128,
                 max_streams: int = 3, idle_seconds: float = 90,
                 startup_wait: float = 18, switch_grace: float = 20,
                 startup_reserve: float = 30):
        self.channels = {c.id: c for c in channels}
        self.order = [c.id for c in channels]
        self.path = playlist_path
        self.private_url = private_url
        self.delay = delay
        self.timeout = timeout
        self.poll = poll
        self.cache_mib = cache_mib
        self.max_streams = max_streams
        self.upstream_slots = threading.BoundedSemaphore(max_streams)
        self.idle_seconds = idle_seconds
        self.startup_wait = startup_wait
        self.startup_reserve = min(startup_reserve, delay)
        self.switch_grace = switch_grace
        self.warmup_successes = 0
        self.warmup_timeouts = 0
        self.stream_limit_denials = 0
        self.recycled_idle_streams = 0
        self.lock = threading.RLock()
        self.active: dict[str, ActiveStream] = {}
        self.stop = threading.Event()
        # The previous single-channel TV playlist pointed at /index.m3u8
        # and /segment/<seq>.ts. Keep those URLs working for one explicitly
        # prewarmed channel while the TV app refreshes its saved catalog.
        self.legacy_channel_id: str | None = None
        self.catalog_requests = 0
        self.legacy_manifest_requests = 0
        self.legacy_segment_requests = 0

    def _new_stream(self, channel: Channel, pinned: bool) -> ActiveStream:
        def refresh() -> str:
            text = private_playlist_text(self.path, self.private_url, self.timeout)
            updated = next((c for c in channels_from_text(text)
                            if c.id == channel.id), None)
            if updated is None:
                raise ValueError("missing_channel_after_refresh")
            return updated.url

        gateway = Gateway(channel.url, delay=self.delay, timeout=self.timeout,
                          poll=self.poll, max_bytes=self.cache_mib * 1048576,
                          source_refresher=refresh,
                          startup_reserve=None if pinned else self.startup_reserve,
                          fast_start=not pinned)
        return ActiveStream(channel, gateway, self.upstream_slots, pinned)

    def start(self, cid: str, *, pinned: bool = False) -> ActiveStream | None:
        """Start a requested channel; do not silently evict active viewers."""
        with self.lock:
            if cid not in self.channels:
                return None
            if cid in self.active:
                stream = self.active[cid]
                stream.accessed = time.monotonic()
                if pinned:
                    stream.pinned = True
                return stream
            self.sweep_locked()
            if len(self.active) >= self.max_streams:
                # A channel-switching TV may keep two stopped channels in
                # cache; reclaim only genuinely idle, unpinned sessions.
                # Never steal from active viewers or pending warmups.
                now = time.monotonic()
                stale = [(sid, item) for sid, item in self.active.items()
                         if not item.pinned and item.pending_requests == 0
                         and now - item.accessed >= self.switch_grace]
                if stale:
                    oldest_id, oldest = min(stale, key=lambda row: row[1].accessed)
                    oldest.close()
                    del self.active[oldest_id]
                    self.recycled_idle_streams += 1
            if len(self.active) >= self.max_streams:
                self.stream_limit_denials += 1
                return None
            stream = self._new_stream(self.channels[cid], pinned)
            self.active[cid] = stream
            return stream

    def active_stream(self, cid: str) -> ActiveStream | None:
        with self.lock:
            stream = self.active.get(cid)
            if stream:
                stream.accessed = time.monotonic()
            return stream

    def sweep_locked(self):
        now = time.monotonic()
        for cid, stream in list(self.active.items()):
            if not stream.pinned and stream.pending_requests == 0 and now - stream.accessed >= self.idle_seconds:
                stream.close()
                del self.active[cid]

    def sweep(self):
        with self.lock:
            self.sweep_locked()

    def cleanup_loop(self):
        while not self.stop.wait(5):
            self.sweep()

    def close(self):
        self.stop.set()
        with self.lock:
            for stream in self.active.values():
                stream.close()
            self.active.clear()

    def status(self) -> bytes:
        with self.lock:
            summaries = []
            for stream in self.active.values():
                details = json.loads(stream.gateway.status())
                summaries.append({
                    "id": stream.channel.id,
                    "channel": stream.channel.name,
                    "pinned": stream.pinned,
                    "ready": details["ready"],
                    "buffer_state": details["buffer_state"],
                    "contiguous_tail_seconds": details["contiguous_tail_seconds"],
                    "effective_buffer_delay_seconds": details["effective_buffer_delay_seconds"],
                    "startup_reserve_seconds": details["startup_reserve_seconds"],
                    "last_published_sequence": details["last_published_sequence"],
                    "seconds_since_published_advance": details["seconds_since_published_advance"],
                    "recovery_mode": details["recovery_mode"],
                    "last_downloaded_sequence": details["last_downloaded_sequence"],
                    "latest_upstream_sequence": details["latest_upstream_sequence"],
                    "seconds_since_new_segment": details["seconds_since_new_segment"],
                    "seconds_since_segment_download": details["seconds_since_segment_download"],
                    "slow_segments": details["slow_segments"],
                    "error_breakdown": details["error_breakdown"],
                    "last_upstream_failure": details["last_upstream_failure"],
                    "seconds_since_last_upstream_failure": details["seconds_since_last_upstream_failure"],
                    "recent_upstream_failures": details["recent_upstream_failures"],
                    "client_manifest_503": details["client_manifest_503"],
                    "client_segment_404": details["client_segment_404"],
                    "recent_client_segment_404": details["recent_client_segment_404"],
                    "pending_requests": stream.pending_requests,
                    "upstream_stalled": details["upstream_stalled"],
                    "errors": details["errors"],
                    "last_error": details["last_error"],
                    "cached_mib": details["cached_mib"],
                    "client_manifest_200": details["client_manifest_200"],
                    "client_segment_200": details["client_segment_200"],
                    "client_segment_206": details["client_segment_206"],
                    "seconds_since_access": round(time.monotonic() - stream.accessed, 1),
                })
            return json.dumps({
                "channels": len(self.channels),
                "active_streams": len(self.active),
                "max_streams": self.max_streams,
                "delay_seconds": self.delay,
                "startup_wait_seconds": self.startup_wait,
                "warmup_successes": self.warmup_successes,
                "warmup_timeouts": self.warmup_timeouts,
                "stream_limit_denials": self.stream_limit_denials,
                "recycled_idle_streams": self.recycled_idle_streams,
                "legacy_channel": (self.channels[self.legacy_channel_id].name
                                   if self.legacy_channel_id in self.channels else None),
                "catalog_requests": self.catalog_requests,
                "legacy_manifest_requests": self.legacy_manifest_requests,
                "legacy_segment_requests": self.legacy_segment_requests,
                "streams": summaries,
            }, ensure_ascii=False).encode("utf-8")

    def playlist(self, token: str, host: str, *, filter_names: list[str] | None = None,
                 public_base_url: str | None = None, epg_enabled: bool = False) -> bytes:
        base_url = public_base_url or f"http://{host}"
        lines = [f'#EXTM3U x-tvg-url="{base_url}/{token}/epg.xml.gz"' if epg_enabled else "#EXTM3U"]
        allowed = set(filter_names) if filter_names is not None else None
        for cid in self.order:
            c = self.channels[cid]
            if allowed is not None and cid not in allowed:
                continue
            name = c.name.replace("\r", " ").replace("\n", " ").replace('"', "")
            group = c.group.replace("\r", " ").replace("\n", " ").replace('"', "")
            meta = f' tvg-id="{cid}"' + (f' group-title="{group}"' if group else "")
            lines.append(f'#EXTINF:-1{meta},{name}')
            lines.append(f'{base_url}/{token}/channel/{cid}/index.m3u8')
        return ("\n".join(lines) + "\n").encode("utf-8")


def make_multi_handler(manager: MultiGateway, token: str,
                       public_base_url: str | None = None,
                       epg_file: Path | None = None):
    prefix = "/" + token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # No token, viewer IP, or upstream URLs in logs.

        def do_HEAD(self):
            self.serve(True)

        def do_GET(self):
            self.serve(False)

        def respond(self, status, body, content_type, head, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def serve(self, head: bool):
            path = urllib.parse.urlsplit(self.path).path
            if not path.startswith(prefix + "/"):
                return self.respond(404, b"Not found", "text/plain", head)
            route = path[len(prefix) + 1:].split("/")
            if route == ["epg.xml.gz"] and epg_file is not None:
                try:
                    body = epg_file.read_bytes()
                    if not body.startswith(b"\x1f\x8b"):
                        return self.respond(503, b"Invalid EPG", "text/plain", head)
                except OSError:
                    return self.respond(503, b"EPG not generated yet", "text/plain", head)
                return self.respond(200, body, "application/gzip", head)
            if route == ["status"]:
                return self.respond(200, manager.status(), "application/json", head)
            if route == ["channels.m3u"]:
                host = self.headers.get("Host", "")
                if not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]{1,255}", host):
                    return self.respond(400, b"Invalid host", "text/plain", head)
                with manager.lock:
                    manager.catalog_requests += 1
                return self.respond(200, manager.playlist(token, host,
                                                          public_base_url=public_base_url,
                                                          epg_enabled=epg_file is not None),
                                    "audio/x-mpegurl", head)
            # Saved TV favorites may retain the old single-channel URLs
            # even after the catalog URL has been reloaded. Alias only to
            # the explicit first --prewarm channel, not any guessed source.
            with manager.lock:
                legacy_id = manager.legacy_channel_id
            if legacy_id is not None and route == ["index.m3u8"]:
                with manager.lock:
                    manager.legacy_manifest_requests += 1
                route = ["channel", legacy_id, "index.m3u8"]
            elif (legacy_id is not None and len(route) == 2
                  and route[0] == "segment" and route[1].endswith(".ts")):
                with manager.lock:
                    manager.legacy_segment_requests += 1
                route = ["channel", legacy_id, "segment", route[1]]
            if len(route) < 3 or route[0] != "channel":
                return self.respond(404, b"Not found", "text/plain", head)
            cid, resource = route[1], route[2]
            if cid not in manager.channels:
                return self.respond(404, b"Unknown channel", "text/plain", head)
            if len(route) == 3 and resource == "index.m3u8":
                stream = manager.start(cid)
                if stream is None:
                    return self.respond(503, b"Too many active streams",
                                        "text/plain", head, {"Retry-After": "10"})
                # The old implementation immediately returned 503 when
                # selecting a cold channel. Many TV players never retry it.
                # Keep this *one* manifest request open briefly while the
                # worker fills its first delayed HLS window.
                with manager.lock:
                    stream.pending_requests += 1
                try:
                    raw = stream.gateway.playlist(token)
                    waited = False
                    deadline = time.monotonic() + manager.startup_wait
                    while raw is None and not manager.stop.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        waited = True
                        if stream.gateway.stop.wait(min(0.25, remaining)):
                            break
                        raw = stream.gateway.playlist(token)
                    with manager.lock:
                        if waited:
                            if raw is not None:
                                manager.warmup_successes += 1
                            else:
                                manager.warmup_timeouts += 1
                finally:
                    with manager.lock:
                        stream.pending_requests -= 1
                        stream.accessed = time.monotonic()
                if raw is None:
                    stream.gateway.record_client("manifest", 503)
                    return self.respond(503, b"Buffer warming up; reselect channel",
                                        "text/plain", head, {"Retry-After": "2"})
                # The legacy single-channel gateway outputs /TOKEN/segment/...
                # Inject this channel's stable ID without touching source URLs.
                before = f"/{token}/segment/".encode()
                after = f"/{token}/channel/{cid}/segment/".encode()
                stream.gateway.record_client("manifest", 200)
                return self.respond(200, raw.replace(before, after),
                                    "application/vnd.apple.mpegurl", head)
            if len(route) == 4 and resource == "segment" and route[3].endswith(".ts"):
                try:
                    seq = int(route[3][:-3])
                except ValueError:
                    return self.respond(404, b"Invalid segment", "text/plain", head)
                # An expired stream is not restarted on a stale segment URL.
                stream = manager.active_stream(cid)
                if stream is None:
                    return self.respond(404, b"Stream expired", "text/plain", head)
                data = stream.gateway.segment(seq, record_miss=True)
                if data is None:
                    return self.respond(404, b"Segment not in cache", "text/plain", head)
                extra = {"Accept-Ranges": "bytes"}
                requested_range = self.headers.get("Range")
                if requested_range:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested_range)
                    if not match or not any(match.groups()):
                        return self.respond(416, b"", "video/mp2t", head,
                                            {"Content-Range": f"bytes */{len(data)}"})
                    if match.group(1):
                        first = int(match.group(1))
                        last = int(match.group(2)) if match.group(2) else len(data) - 1
                    else:
                        suffix = int(match.group(2))
                        first, last = max(0, len(data) - suffix), len(data) - 1
                    if first >= len(data) or last < first:
                        return self.respond(416, b"", "video/mp2t", head,
                                            {"Content-Range": f"bytes */{len(data)}"})
                    last = min(last, len(data) - 1)
                    extra["Content-Range"] = f"bytes {first}-{last}/{len(data)}"
                    stream.gateway.record_client("segment", 206)
                    return self.respond(206, data[first:last + 1], "video/mp2t",
                                        head, extra)
                stream.gateway.record_client("segment", 200)
                return self.respond(200, data, "video/mp2t", head, extra)
            return self.respond(404, b"Not found", "text/plain", head)

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="On-demand multi-channel LAN IPTV gateway")
    p.add_argument("--playlist", type=Path, required=True)
    p.add_argument("--delay", type=int, default=30)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--advertise-host", default=None)
    p.add_argument("--public-base-url", default=None,
                   help="HTTPS origin advertised in M3U links when served behind a TLS reverse proxy")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--timeout", type=float, default=12)
    p.add_argument("--poll", type=float, default=2)
    p.add_argument("--cache-mib", type=int, default=128,
                   help="Per-active-channel cache limit in MiB")
    p.add_argument("--max-streams", type=int, default=3)
    p.add_argument("--idle-seconds", type=float, default=90)
    p.add_argument("--startup-wait", type=float, default=18,
                   help="Hold a cold channel's first manifest request up to N seconds")
    p.add_argument("--startup-reserve", type=float, default=30,
                   help="Preferred extra cached video during the first 3 seconds of a cold start (default 30s, capped at --delay)")
    p.add_argument("--switch-grace", type=float, default=20,
                   help="At stream limit, reclaim non-pinned channels idle for N seconds")
    p.add_argument("--prewarm", action="append", default=[],
                   help="Exact channel name to keep buffered; repeatable")
    p.add_argument("--filter", action="append", default=[],
                   help="Show only channels whose names contain this text; repeatable")
    p.add_argument("--token-file", type=Path, default=Path(".iptv_gateway_token"))
    p.add_argument("--epg-file", type=Path, default=None,
                   help="Generated private XMLTV gzip file served at /TOKEN/epg.xml.gz")
    p.add_argument("--playlist-url-env", default=None)
    args = p.parse_args(argv)
    if not (15 <= args.delay <= 120 and args.poll > 0 and args.timeout > 0
            and 32 <= args.cache_mib <= 2048 and 1 <= args.max_streams <= 8
            and args.idle_seconds >= 15 and 0 <= args.startup_wait <= 60
            and 0 < args.startup_reserve <= 120
            and 10 <= args.switch_grace <= args.idle_seconds
            and 1 <= args.port <= 65535):
        p.error("Invalid delay, poll, timeout, cache-mib, max-streams, idle-seconds, startup-wait, startup-reserve, switch-grace, or port")
    if args.public_base_url and not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?", args.public_base_url):
        p.error("--public-base-url must be an HTTPS origin without a path, e.g. https://tv.example.com")
    public_base_url = args.public_base_url.rstrip("/") if args.public_base_url else None
    private_url = os.environ.get(args.playlist_url_env) if args.playlist_url_env else None
    if args.playlist_url_env and not private_url:
        p.error("Playlist URL environment variable was not set")

    # Prefer a fresh authorized playlist on startup; fall back to the local
    # private M3U if the source service is temporarily unavailable.
    channels = []
    if private_url:
        try:
            source = private_playlist_text(args.playlist, private_url, args.timeout)
            channels = channels_from_text(source)
            if not channels:
                print("Remote playlist contains no channels; using local M3U.")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            print(f"Remote playlist unavailable ({safe_error(exc)}); using local M3U.")
    if not channels:
        try:
            channels = channels_from_entries(entries_from_m3u(args.playlist))
        except (OSError, ValueError):
            p.error("Could not read local M3U playlist")
    if args.filter:
        patterns = [x.casefold() for x in args.filter]
        channels = [c for c in channels if any(q in c.name.casefold() for q in patterns)]
    if not channels:
        p.error("No channels found")
    if len({c.id for c in channels}) != len(channels):
        p.error("Channel ID collision")
    manager = MultiGateway(channels, args.playlist, private_url, args.delay,
                           args.timeout, args.poll, args.cache_mib,
                           args.max_streams, args.idle_seconds,
                           args.startup_wait, args.switch_grace, args.startup_reserve)
    try:
        token = load_or_create_token(args.token_file)
    except (OSError, UnicodeError, ValueError):
        p.error("Could not load persistent token")
    prewarm = []
    for name in args.prewarm:
        found = [c for c in channels if c.name.casefold() == name.casefold()]
        if len(found) != 1:
            p.error(f"Expected exactly one prewarm channel named {name!r}, got {len(found)}")
        prewarm.append(found[0].id)
    if len(set(prewarm)) > args.max_streams:
        p.error("More prewarm channels than max-streams")
    # Preserve old /TOKEN/index.m3u8 and /TOKEN/segment/NN.ts links
    # only when the operator explicitly selects a known legacy channel.
    if prewarm:
        manager.legacy_channel_id = prewarm[0]
    for cid in dict.fromkeys(prewarm):
        manager.start(cid, pinned=True)
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_multi_handler(manager, token, public_base_url, args.epg_file))
    server.daemon_threads = True
    sweep_thread = threading.Thread(target=manager.cleanup_loop, daemon=True)
    sweep_thread.start()
    shown = args.advertise_host or ("<PC_LAN_IP>" if args.host == "0.0.0.0" else args.host)
    print(f"Loaded {len(channels)} channels; prewarming {len(prewarm)}. Source URLs hidden.")
    shown_base = public_base_url or f"http://{shown}:{args.port}"
    print(f"TV playlist URL: {shown_base}/{token}/channels.m3u")
    print(f"Status: {shown_base}/{token}/status")
    if args.epg_file:
        print(f"EPG URL: {shown_base}/{token}/epg.xml.gz")
    print("Cold channels take time to buffer; prewarm favorites. Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
