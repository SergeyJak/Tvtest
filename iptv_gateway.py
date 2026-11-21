#!/usr/bin/env python3
"""Single-channel LAN HLS cache with configurable delayed playback.

Designed as a diagnostic experiment, not a public IPTV redistribution service.
No subscriptions or upstream URLs are ever printed or saved.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from iptv_healthcheck import UA, entries_from_m3u


@dataclass(frozen=True)
class Segment:
    sequence: int
    duration: float
    url: str


def fetch_text(url: str, timeout: float) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(1024 * 1024 + 1)
        final_url = response.geturl()
    if len(data) > 1024 * 1024:
        raise ValueError('playlist_too_large')
    return data.decode('utf-8-sig', errors='replace'), final_url


def media_manifest(url: str, timeout: float) -> tuple[str, str]:
    for _ in range(3):
        body, base = fetch_text(url, timeout)
        if '#EXT-X-STREAM-INF' not in body:
            return body, base
        lines = body.splitlines()
        variant = next((lines[j].strip() for i, line in enumerate(lines)
                        if line.startswith('#EXT-X-STREAM-INF')
                        for j in range(i + 1, len(lines))
                        if lines[j].strip() and not lines[j].startswith('#')), None)
        if not variant:
            raise ValueError('missing_variant')
        url = urllib.parse.urljoin(base, variant)
    raise ValueError('too_many_master_playlists')


def parse_media(text: str, base: str) -> tuple[float, list[Segment]]:
    if not text.lstrip('\ufeff').startswith('#EXTM3U'):
        raise ValueError('not_hls')
    # Encryption, fMP4 maps, byte ranges and discontinuities require their own
    # tags/key handling. Never silently publish broken HLS output.
    unsupported = ('#EXT-X-KEY:', '#EXT-X-MAP:', '#EXT-X-BYTERANGE:',
                   '#EXT-X-DISCONTINUITY', '#EXT-X-PART:', '#EXT-X-PRELOAD-HINT:')
    if any(line.strip().startswith(unsupported) for line in text.splitlines()):
        raise ValueError('unsupported_hls_features')
    sequence, duration, target = 0, None, 6.0
    segments = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-TARGETDURATION:'):
            target = max(1.0, float(line.split(':', 1)[1]))
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',', 1)[0])
        elif line and not line.startswith('#') and duration is not None:
            url = urllib.parse.urljoin(base, line)
            if urllib.parse.urlparse(url).scheme not in ('http', 'https'):
                raise ValueError('unsupported_segment_scheme')
            segments.append(Segment(sequence + len(segments), duration, url))
            duration = None
    return target, segments


def fetch_segment(url: str, timeout: float, max_bytes: int) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise ValueError('empty_or_oversize_segment')
    return data


def safe_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP_{exc.code}'
    if isinstance(exc, urllib.error.URLError):
        return 'NETWORK_ERROR'
    if isinstance(exc, TimeoutError):
        return 'TIMEOUT'
    if isinstance(exc, ValueError):
        return str(exc) if str(exc) in {
            'not_hls', 'unsupported_hls_features', 'empty_or_oversize_segment',
            'unsupported_segment_scheme', 'playlist_too_large',
            'missing_variant', 'too_many_master_playlists'
        } else 'INVALID_HLS'
    return type(exc).__name__


class Gateway:
    def __init__(self, url: str, delay: float = 45, timeout: float = 12,
                 poll: float = 2, max_bytes: int = 128 * 1024 * 1024,
                 max_segment: int = 32 * 1024 * 1024,
                 source_refresher: Callable[[], str] | None = None,
                 refresh_cooldown: float = 20):
        self._url = url
        self.source_refresher = source_refresher
        self.refresh_cooldown = refresh_cooldown
        self.last_refresh_monotonic = None
        self.source_refresh_attempts = 0
        self.source_refresh_errors = 0
        self.source_changes = 0
        self.last_source_change = None
        self.source_refresh_error = None
        self.consecutive_manifest_404 = 0
        self.delay, self.timeout, self.poll = delay, timeout, poll
        self.max_bytes, self.max_segment = max_bytes, max_segment
        self.lock = threading.RLock()
        self.cache: OrderedDict[int, tuple[float, bytes]] = OrderedDict()
        self.total_bytes = 0
        self.latest = None
        self.target = 6.0
        self.last_error = None
        self.slow_segments = 0
        self.errors = 0
        self.last_update = None
        self.last_progress = None
        self.last_segment_download = None
        self.client_manifest_503 = 0
        self.client_segment_404 = 0
        self.client_auth_404 = 0
        self.client_playlist_200 = 0
        self.client_manifest_200 = 0
        self.client_segment_200 = 0
        self.client_segment_206 = 0
        self.last_client_playlist_access = None
        self.last_client_manifest_access = None
        self.last_client_segment_access = None
        self.last_client_auth_failure = None
        self.last_advance_at = None
        self.last_published_seq = None
        self.stop = threading.Event()

    def refresh_source(self) -> bool:
        """Try to resolve a fresh stream URL; never expose the subscription URL.

        When a private playlist URL is configured, fetch it again. Otherwise
        re-read the local playlist if the user has saved an updated copy.
        """
        if self.source_refresher is None:
            return False
        now = time.monotonic()
        with self.lock:
            if (self.last_refresh_monotonic is not None
                    and now - self.last_refresh_monotonic < self.refresh_cooldown):
                return False
            self.last_refresh_monotonic = now
            self.source_refresh_attempts += 1
        try:
            candidate = self.source_refresher()
            if not candidate or urllib.parse.urlsplit(candidate).scheme not in ('http', 'https'):
                raise ValueError('invalid_refreshed_url')
        except (OSError, UnicodeError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            with self.lock:
                self.source_refresh_errors += 1
                self.source_refresh_error = safe_error(exc)
            return False
        with self.lock:
            self.source_refresh_error = None
            if candidate == self._url:
                return False  # Same upstream: a 404 still requires provider recovery.
            self._url = candidate
            self.source_changes += 1
            self.last_source_change = time.time()
            # Retain existing buffered segments for playback while reconnecting.
            # cycle() handles large upstream sequence resets before caching.
            return True

    def cycle(self):
        try:
            body, base = media_manifest(self._url, self.timeout)
            target, listed = parse_media(body, base)
            if not listed:
                raise ValueError('empty_playlist')
            with self.lock:
                if self.latest is not None and listed[-1].sequence < self.latest - 10:
                    self.cache.clear()
                    self.total_bytes = 0
                    self.last_published_seq = None
                if self.latest is None or self.latest != listed[-1].sequence:
                    self.last_progress = time.time()
                    self.last_advance_at = time.monotonic()
                self.latest = listed[-1].sequence
                self.target = target
                self.last_update = time.time()
                self.last_error = None
                self.consecutive_manifest_404 = 0
            for segment in listed:
                if self.stop.is_set():
                    return
                with self.lock:
                    present = segment.sequence in self.cache
                if present:
                    continue
                start = time.monotonic()
                try:
                    data = fetch_segment(segment.url, self.timeout, self.max_segment)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    with self.lock:
                        self.errors += 1
                        self.last_error = 'SEGMENT_' + safe_error(exc)
                    continue
                duration = time.monotonic() - start
                with self.lock:
                    if duration >= segment.duration:
                        self.slow_segments += 1
                    if segment.sequence not in self.cache:
                        self.last_segment_download = time.time()
                        self.cache[segment.sequence] = (segment.duration, data)
                        self.cache = OrderedDict(sorted(self.cache.items()))
                        self.total_bytes += len(data)
                    self._evict()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            with self.lock:
                self.errors += 1
                self.last_error = 'MANIFEST_' + safe_error(exc)
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    self.consecutive_manifest_404 += 1
                else:
                    self.consecutive_manifest_404 = 0
            # 404 may indicate a rotated/expired stream URL. Re-resolve it from
            # the user's authorized playlist, but never manufacture new URLs.
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                self.refresh_source()

    def _evict(self):
        duration = sum(item[0] for item in self.cache.values())
        while self.cache and (duration > max(self.delay + 150, 180) or self.total_bytes > self.max_bytes):
            _, (seconds, data) = self.cache.popitem(last=False)
            duration -= seconds
            self.total_bytes -= len(data)

    def run(self):
        while not self.stop.is_set():
            start = time.monotonic()
            self.cycle()
            self.stop.wait(max(0.2, self.poll - (time.monotonic() - start)))

    def _contiguous_tail(self) -> list[tuple[int, float]]:
        """The newest uninterrupted run of locally downloaded segments."""
        items = [(seq, entry[0]) for seq, entry in self.cache.items()]
        if not items:
            return []
        tail = [items[-1]]
        for entry in reversed(items[:-1]):
            if entry[0] + 1 != tail[0][0]:
                break
            tail.insert(0, entry)
        return tail

    def published(self, commit: bool = True) -> list[tuple[int, float]]:
        """Publish playable segments, recovering after a hole in the cache.

        During a healthy stream, stay --delay seconds behind the latest
        downloaded segment. A missing segment can split the cache into
        discontinuous runs. Once a player has started, return a small playable
        *newer* run rather than HTTP 503 forever while the full delayed window
        refills. This temporarily reduces the effective delay.
        """
        with self.lock:
            if not self.cache or self.latest is None:
                return []
            tail = self._contiguous_tail()
            # At startup, demand a real full delay window before publishing.
            # During recovery, three contiguous segments can resume playback.
            if len(tail) < 3:
                return []
            if self.last_published_seq is not None and tail[-1][0] < self.last_published_seq:
                return []  # Never silently rewind the HLS media sequence.
            elapsed = max(0.0, time.monotonic() - self.last_advance_at) if self.last_advance_at is not None else 0.0
            required_behind = max(0.0, self.delay - elapsed)
            remaining = 0.0
            cutoff = -1
            for i in range(len(tail) - 1, -1, -1):
                if remaining >= required_behind:
                    cutoff = i
                    break
                remaining += tail[i][1]
            if cutoff < 2:
                if self.last_published_seq is None:
                    return []  # New TV session must warm up before playback.
                # Resume from a newer local run after an upstream segment gap.
                cutoff = 2
            if self.last_published_seq is not None:
                forward_index = next((i for i, (seq, _) in enumerate(tail)
                                      if seq >= self.last_published_seq), None)
                if forward_index is not None:
                    cutoff = max(cutoff, forward_index)
            if commit:
                self.last_published_seq = tail[cutoff][0]
            return tail[max(0, cutoff - 7):cutoff + 1]

    def playlist(self, token: str) -> bytes | None:
        records = self.published()
        if not records:
            return None
        target = max(1, math.ceil(max(s for _, s in records)))
        lines = ['#EXTM3U', '#EXT-X-VERSION:3', f'#EXT-X-TARGETDURATION:{target}',
                 f'#EXT-X-MEDIA-SEQUENCE:{records[0][0]}']
        for sequence, duration in records:
            lines.extend((f'#EXTINF:{duration:.3f},', f'/{token}/segment/{sequence}.ts'))
        return ('\n'.join(lines) + '\n').encode('utf-8')

    def segment(self, seq: int) -> bytes | None:
        with self.lock:
            found = self.cache.get(seq)
            return found[1] if found is not None else None

    def record_client(self, kind: str, status: int) -> None:
        """Count client HTTP responses without storing IPs, paths, or tokens."""
        now = time.time()
        with self.lock:
            if kind == 'auth' and status == 404:
                self.client_auth_404 += 1
                self.last_client_auth_failure = now
            elif kind == 'playlist' and status == 200:
                self.client_playlist_200 += 1
                self.last_client_playlist_access = now
            elif kind == 'manifest':
                if status == 200:
                    self.client_manifest_200 += 1
                elif status == 503:
                    self.client_manifest_503 += 1
                self.last_client_manifest_access = now
            elif kind == 'segment':
                if status == 200:
                    self.client_segment_200 += 1
                elif status == 206:
                    self.client_segment_206 += 1
                elif status == 404:
                    self.client_segment_404 += 1
                self.last_client_segment_access = now

    def status(self) -> bytes:
        with self.lock:
            # Read-only observation must not move the TV's playback cursor.
            playable = self.published(commit=False)
            tail = self._contiguous_tail()
            tail_seconds = round(sum(duration for _, duration in tail), 1)
            behind_seconds = round(sum(duration for seq, duration in tail
                                       if playable and seq > playable[-1][0]), 1) if playable else None
            state = ('READY' if playable else 'WAITING_FOR_VIDEO' if not self.cache
                     else 'WARMING_OR_REBUILDING_BUFFER')
            info = dict(ready=bool(playable), delay_seconds=self.delay,
                        buffer_state=state,
                        contiguous_tail_segments=len(tail),
                        contiguous_tail_seconds=tail_seconds,
                        effective_buffer_delay_seconds=behind_seconds,
                        last_published_sequence=self.last_published_seq,
                        last_downloaded_sequence=next(reversed(self.cache), None) if self.cache else None,
                        latest_upstream_sequence=self.latest,
                        client_manifest_503=self.client_manifest_503,
                        client_segment_404=self.client_segment_404,
                        client_playlist_200=self.client_playlist_200,
                        client_manifest_200=self.client_manifest_200,
                        client_segment_200=self.client_segment_200,
                        client_segment_206=self.client_segment_206,
                        client_auth_404=self.client_auth_404,
                        seconds_since_client_playlist=round(time.time()-self.last_client_playlist_access, 1) if self.last_client_playlist_access else None,
                        seconds_since_client_manifest=round(time.time()-self.last_client_manifest_access, 1) if self.last_client_manifest_access else None,
                        seconds_since_client_segment=round(time.time()-self.last_client_segment_access, 1) if self.last_client_segment_access else None,
                        seconds_since_client_auth_failure=round(time.time()-self.last_client_auth_failure, 1) if self.last_client_auth_failure else None,
                        seconds_since_segment_download=round(time.time() - self.last_segment_download, 1) if self.last_segment_download else None,
                        cached_segments=len(self.cache), cached_mib=round(self.total_bytes / 1048576, 2),
                        errors=self.errors, slow_segments=self.slow_segments,
                        last_error=self.last_error,
                        consecutive_manifest_404=self.consecutive_manifest_404,
                        source_refresh_attempts=self.source_refresh_attempts,
                        source_refresh_errors=self.source_refresh_errors,
                        source_refresh_error=self.source_refresh_error,
                        source_changes=self.source_changes,
                        seconds_since_source_change=round(time.time() - self.last_source_change, 1) if self.last_source_change else None,
                        seconds_since_playlist_update=round(time.time() - self.last_update, 1) if self.last_update else None,
                        seconds_since_new_segment=round(time.time() - self.last_progress, 1) if self.last_progress else None,
                        upstream_stalled=bool(self.last_progress and time.time() - self.last_progress > max(15, 3 * self.target)))
        return json.dumps(info, ensure_ascii=False).encode('utf-8')


def make_handler(gateway: Gateway, token: str, channel_name: str):
    prefix = '/' + token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # URLs may contain access token; do not log HTTP request lines.

        def do_HEAD(self):
            self.serve(head_only=True)

        def do_GET(self):
            self.serve(head_only=False)

        def respond(self, data: bytes, mimetype: str, head_only: bool, status=200, extra=None):
            self.send_response(status)
            self.send_header('Content-Type', mimetype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store' if mimetype.endswith('mpegurl') else 'private, max-age=60')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head_only:
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def serve(self, head_only: bool):
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith(prefix + '/'):
                gateway.record_client('auth', 404)
                return self.respond(b'Not found', 'text/plain', head_only, 404)
            part = path[len(prefix):]
            if part == '/status':
                return self.respond(gateway.status(), 'application/json; charset=utf-8', head_only)
            if part == '/channels.m3u':
                # A single-channel playlist suitable for IPTV player imports.
                clean_name = channel_name.replace('\r', ' ').replace('\n', ' ').replace('"', '')
                host = self.headers.get('Host', '127.0.0.1:8787').split('/')[0]
                if any(ch in host for ch in ('\r', '\n', ' ')):
                    return self.respond(b'Bad host', 'text/plain', head_only, 400)
                content = f'#EXTM3U\n#EXTINF:-1,{clean_name}\nhttp://{host}{prefix}/index.m3u8\n'
                gateway.record_client('playlist', 200)
                return self.respond(content.encode('utf-8'), 'audio/x-mpegurl', head_only)
            if part == '/index.m3u8':
                content = gateway.playlist(token)
                if content is None:
                    gateway.record_client('manifest', 503)
                    return self.respond(b'Buffer warming up', 'text/plain', head_only, 503,
                                        {'Retry-After': '5'})
                gateway.record_client('manifest', 200)
                return self.respond(content, 'application/vnd.apple.mpegurl', head_only)
            if part.startswith('/segment/') and part.endswith('.ts'):
                try:
                    seq = int(part[len('/segment/'):-3])
                except ValueError:
                    return self.respond(b'Not found', 'text/plain', head_only, 404)
                data = gateway.segment(seq)
                if data is None:
                    gateway.record_client('segment', 404)
                    return self.respond(b'Segment expired', 'text/plain', head_only, 404)
                headers = {'Accept-Ranges': 'bytes'}
                range_header = self.headers.get('Range')
                if range_header:
                    import re
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
                    if not match or not any(match.groups()):
                        return self.respond(b'', 'video/mp2t', head_only, 416,
                                            {'Content-Range': f'bytes */{len(data)}'})
                    if match.group(1):
                        start = int(match.group(1))
                        end = int(match.group(2)) if match.group(2) else len(data) - 1
                    else:
                        suffix = int(match.group(2))
                        start, end = max(0, len(data) - suffix), len(data) - 1
                    if start >= len(data) or end < start:
                        return self.respond(b'', 'video/mp2t', head_only, 416,
                                            {'Content-Range': f'bytes */{len(data)}'})
                    end = min(len(data) - 1, end)
                    headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
                    gateway.record_client('segment', 206)
                    return self.respond(data[start:end + 1], 'video/mp2t', head_only, 206, headers)
                gateway.record_client('segment', 200)
                return self.respond(data, 'video/mp2t', head_only, extra=headers)
            return self.respond(b'Not found', 'text/plain', head_only, 404)
    return Handler


def playlist_entry_from_text(text: str, channel_name: str) -> str:
    """Find one exact channel without writing credentials to disk or logs."""
    current = None
    matches = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            current = line.rsplit(',', 1)[-1].strip()
        elif line and not line.startswith('#') and current is not None:
            if current.casefold() == channel_name.casefold():
                matches.append(line)
            current = None
    if len(matches) != 1:
        raise ValueError('channel_match_count_invalid')
    return matches[0]


def private_playlist_text(path: Path, source_url: str | None, timeout: float) -> str:
    if source_url is None:
        return path.read_text(encoding='utf-8-sig')
    if urllib.parse.urlsplit(source_url).scheme not in ('http', 'https'):
        raise ValueError('invalid_playlist_scheme')
    request = urllib.request.Request(source_url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        contents = response.read(2 * 1024 * 1024 + 1)
    if len(contents) > 2 * 1024 * 1024:
        raise ValueError('private_playlist_too_large')
    return contents.decode('utf-8-sig')


def load_or_create_token(path: Path) -> str:
    """Keep LAN playlist URLs stable without storing credentials in Git.

    Existing tokens may be migrated manually from a previous TV URL to avoid
    reconfiguring clients when upgrading from the ephemeral-token version.
    """
    import re

    def valid(value: str) -> str:
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value):
            raise ValueError('invalid_token_file')
        return value

    try:
        # Exclusive creation prevents accidentally replacing an existing key.
        with path.open('x', encoding='ascii') as file:
            token = secrets.token_urlsafe(24)
            file.write(token + '\n')
        if os.name != 'nt':
            path.chmod(0o600)
        return token
    except FileExistsError:
        with path.open('r', encoding='ascii') as file:
            token = file.read(256).strip()
        return valid(token)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local HLS buffering gateway: single channel')
    parser.add_argument('--playlist', required=True, type=Path)
    parser.add_argument('--channel', required=True, help='Exact case-insensitive channel name')
    parser.add_argument('--delay', type=int, default=45, help='Playback delay in seconds (default: 45)')
    parser.add_argument('--host', default='127.0.0.1', help='Use 0.0.0.0 for access from LAN TVs')
    parser.add_argument('--advertise-host', help='PC LAN IP, used only for printed URL')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--timeout', type=float, default=12)
    parser.add_argument('--poll', type=float, default=2)
    parser.add_argument('--cache-mib', type=int, default=256,
                        help='Maximum segment cache memory in MiB (default 256; 128 was too small for 90-second delayed streams)')
    parser.add_argument('--token-file', type=Path, default=Path('.iptv_gateway_token'),
                        help='Local private token file; stable playlist link across restarts')
    parser.add_argument('--playlist-url-env', default=None,
                        help='Optional name of environment variable containing private Edem playlist URL; never pass the URL itself')
    args = parser.parse_args(argv)
    if (args.delay < 15 or args.delay > 120 or args.timeout <= 0 or args.poll <= 0
            or not 64 <= args.cache_mib <= 2048 or not 1 <= args.port <= 65535):
        parser.error('delay must be 15..120, timeout/poll > 0, cache-mib 64..2048, port 1..65535')
    try:
        entries = entries_from_m3u(args.playlist)
    except (OSError, UnicodeError):
        parser.error('Could not load local playlist')
    matches = [entry for entry in entries if entry[0].casefold() == args.channel.casefold()]
    if len(matches) != 1:
        parser.error(f'Expected exactly one exact channel match; got {len(matches)}')
    private_url = None
    if args.playlist_url_env:
        private_url = os.environ.get(args.playlist_url_env)
        if not private_url:
            parser.error('Playlist URL environment variable was not set')
    def source_refresher():
        text = private_playlist_text(args.playlist, private_url, args.timeout)
        return playlist_entry_from_text(text, args.channel)
    gateway = Gateway(matches[0][2], delay=args.delay, timeout=args.timeout, poll=args.poll,
                      max_bytes=args.cache_mib * 1024 * 1024,
                      source_refresher=source_refresher)
    try:
        token = load_or_create_token(args.token_file)
    except (OSError, UnicodeError, ValueError):
        parser.error('Could not load or create a valid private gateway token file')
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(gateway, token, matches[0][0]))
    httpd.daemon_threads = True
    thread = threading.Thread(target=gateway.run, daemon=True)
    thread.start()
    shown_host = args.advertise_host or ('<PC_LAN_IP>' if args.host == '0.0.0.0' else args.host)
    print(f'Buffering {matches[0][0]} with {args.delay}s delay. URLs to source are hidden.')
    print(f'TV playlist URL: http://{shown_host}:{args.port}/{token}/channels.m3u')
    print(f'Status: http://{shown_host}:{args.port}/{token}/status')
    print('Start playback after status says ready:true. Keep this PC running. Ctrl+C to stop.')
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('Stopping gateway.')
    finally:
        gateway.stop.set()
        httpd.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
