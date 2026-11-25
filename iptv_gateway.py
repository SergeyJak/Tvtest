#!/usr/bin/env python3
"""Single-channel LAN HLS cache with configurable delayed playback.

Designed as a diagnostic experiment, not a public IPTV redistribution service.
No subscriptions or upstream URLs are ever printed or saved.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict, deque
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
        raw_length = response.headers.get('Content-Length')
        expected = int(raw_length) if raw_length is not None and raw_length.isdecimal() else None
        if expected is not None and expected > max_bytes:
            raise ValueError('empty_or_oversize_segment')
        data = response.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise ValueError('empty_or_oversize_segment')
    if expected is not None and len(data) != expected:
        raise ValueError('truncated_segment')
    return data


def safe_error(exc: Exception) -> str:
    if isinstance(exc, http.client.IncompleteRead):
        return 'INCOMPLETE_READ'
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
            'missing_variant', 'too_many_master_playlists', 'truncated_segment'
        } else 'INVALID_HLS'
    return type(exc).__name__


class Gateway:
    def __init__(self, url: str, delay: float = 45, timeout: float = 12,
                 poll: float = 2, max_bytes: int = 128 * 1024 * 1024,
                 max_segment: int = 32 * 1024 * 1024,
                 source_refresher: Callable[[], str] | None = None,
                 refresh_cooldown: float = 20, startup_reserve: float | None = None,
                 fast_start: bool = False,
                 on_event: Callable[[str, str, float | None], None] | None = None,
                 require_startup_reserve: bool = False):
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
        self.manifest_failure_streak = 0
        self.segment_failure_streak = 0
        self.manifest_backoff_until = 0.0
        self._pending_source_reset = False
        self.delay, self.timeout, self.poll = delay, timeout, poll
        self.startup_reserve = delay if startup_reserve is None else min(startup_reserve, delay)
        self.require_startup_reserve = require_startup_reserve
        self._fast_start_at = time.monotonic() + 3.0 if fast_start and not require_startup_reserve else None
        self.max_bytes, self.max_segment = max_bytes, max_segment
        self.lock = threading.RLock()
        self.cache_epoch = 0
        self.cache: OrderedDict[int, tuple[float, bytes]] = OrderedDict()
        self.total_bytes = 0
        self.latest = None
        self.target = 6.0
        self.last_error = None
        self.slow_segments = 0
        self.errors = 0
        # Keep sanitized failure history even after a successful manifest
        # resets last_error. Never store source URLs, keys or exception text.
        self.error_breakdown = Counter()
        self.recent_upstream_failures = deque(maxlen=8)
        self.last_upstream_failure_at = None
        self.last_upstream_failure = None
        self.last_update = None
        self.last_progress = None
        self.last_segment_download = None
        self.client_manifest_503 = 0
        self.client_segment_404 = 0
        self.recent_client_segment_404 = deque(maxlen=8)
        self.client_auth_404 = 0
        self.client_playlist_200 = 0
        self.client_manifest_200 = 0
        self.client_segment_200 = 0
        self.client_segment_206 = 0
        self.last_client_playlist_access = None
        self.last_client_manifest_access = None
        self.last_client_segment_access = None
        self.last_client_auth_failure = None
        self.last_published_seq = None
        self._published_start = None
        self._last_published_at = None
        # After a discontinuous recovery window, temporarily publish closer to
        # live instead of waiting for an unattainable full 90-second backlog.
        self._recovery_mode = False
        self.stop = threading.Event()
        self.on_event = on_event
        self._stall_since: float | None = None

    def _notify_event(self, kind: str, detail: str = "", duration: float | None = None):
        if self.on_event:
            try:
                self.on_event(kind, detail, duration)
            except Exception:
                pass  # Telemetry must never break video delivery.

    def check_stall(self):
        """Report each loss of upstream sequence advancement once, then recovery."""
        now = time.time()
        event = None
        with self.lock:
            progress = self.last_progress
            downloaded = self.last_segment_download
            threshold = max(15.0, 3 * self.target)
            upstream_stale = progress is not None and now - progress > threshold
            download_stale = downloaded is not None and now - downloaded > threshold
            if upstream_stale or download_stale:
                if self._stall_since is None:
                    stalled_since = min(
                        value for value, stale in ((progress, upstream_stale),
                                                   (downloaded, download_stale)) if stale
                    )
                    self._stall_since = stalled_since
                    reason = ("NO_DOWNLOADED_SEGMENTS" if download_stale
                              else "UPSTREAM_NO_PROGRESS")
                    event = ("stream_stalled", reason, round(now - stalled_since, 1))
            elif self._stall_since is not None:
                elapsed = round(now - self._stall_since, 1)
                self._stall_since = None
                event = ("stream_recovered", "UPSTREAM_PROGRESS_RESTORED", elapsed)
        if event:
            self._notify_event(*event)

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
        except (OSError, UnicodeError, urllib.error.URLError, TimeoutError, ValueError, http.client.IncompleteRead) as exc:
            with self.lock:
                self.source_refresh_errors += 1
                self.source_refresh_error = safe_error(exc)
            self._notify_event("upstream_error", "SOURCE_REFRESH_" + safe_error(exc))
            return False
        with self.lock:
            self.source_refresh_error = None
            if candidate == self._url:
                return False  # Same upstream: a 404 still requires provider recovery.
            self._url = candidate
            # HLS sequence numbers may overlap between two distinct URLs.
            # Invalidate the old cache only after the new URL yields valid HLS.
            self._pending_source_reset = True
            self.manifest_backoff_until = 0.0
            self.manifest_failure_streak = 0
            self.source_changes += 1
            self.last_source_change = time.time()
            # Retain existing buffered segments for playback while reconnecting.
            # cycle() handles large upstream sequence resets before caching.
            return True

    def record_upstream_failure(self, kind: str, exc: Exception,
                                sequence: int | None = None) -> None:
        """Count a sanitized upstream HTTP/segment failure, without URL leakage."""
        label = kind + '_' + safe_error(exc)
        now = time.time()
        with self.lock:
            self.errors += 1
            self.last_error = label
            self.error_breakdown[label] += 1
            self.last_upstream_failure_at = now
            self.last_upstream_failure = label
            self.recent_upstream_failures.append({
                'utc_unix': round(now, 1),
                'type': label,
                'sequence': sequence,
            })
        self._notify_event("upstream_error", label)

    def cycle(self):
        # 404/502 bursts used to poll a broken provider manifest every 2 sec.
        # Bound retries while still checking regularly for a recovered feed.
        if time.monotonic() < self.manifest_backoff_until:
            return
        try:
            body, base = media_manifest(self._url, self.timeout)
            target, listed = parse_media(body, base)
            if not listed:
                raise ValueError('empty_playlist')
            with self.lock:
                if self._pending_source_reset:
                    # Never mix identical HLS sequence numbers from two
                    # upstream URLs. The old cache is kept until validation.
                    self.cache_epoch += 1
                    self.cache.clear()
                    self.total_bytes = 0
                    self.latest = None
                    self.last_published_seq = None
                    self._published_start = None
                    self._last_published_at = None
                    self._recovery_mode = False
                    self._pending_source_reset = False
                    if not self.require_startup_reserve:
                        self._fast_start_at = time.monotonic() + 3.0
                    self.segment_failure_streak = 0
                self.manifest_failure_streak = 0
                self.manifest_backoff_until = 0.0
                if self.latest is not None and listed[-1].sequence < self.latest - 10:
                    self.cache_epoch += 1
                    self.cache.clear()
                    self.total_bytes = 0
                    self.last_published_seq = None
                    self._published_start = None
                    self._last_published_at = None
                    self._recovery_mode = False
                if self.latest is None or self.latest != listed[-1].sequence:
                    self.last_progress = time.time()
                self.latest = listed[-1].sequence
                self.target = target
                self.last_update = time.time()
                self.last_error = None
                self.consecutive_manifest_404 = 0
            pending = deque(listed)
            from_live_edge = self.segment_failure_streak >= 3
            consecutive_misses = 0
            while pending:
                segment = pending.pop() if from_live_edge else pending.popleft()
                if self.stop.is_set():
                    return
                with self.lock:
                    present = segment.sequence in self.cache
                if present:
                    continue
                start = time.monotonic()
                try:
                    data = fetch_segment(segment.url, self.timeout, self.max_segment)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.IncompleteRead) as exc:
                    self.record_upstream_failure('SEGMENT', exc, segment.sequence)
                    with self.lock:
                        self.segment_failure_streak += 1
                        failures = self.segment_failure_streak
                    code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
                    # A valid manifest with expired segment URLs can also
                    # indicate the provider has rotated the channel stream.
                    if failures >= 2 and code in (404, 500, 502, 503, 504):
                        if self.refresh_source():
                            break  # Retry using the freshly resolved HLS URL.
                    # The CDN can expire the front of a sliding window while
                    # its newest segments still play. Give those a turn.
                    consecutive_misses += 1
                    if consecutive_misses >= 3:
                        break  # Avoid a long queue of dead segment downloads.
                    if consecutive_misses == 2:
                        from_live_edge = True
                    continue
                consecutive_misses = 0
                duration = time.monotonic() - start
                with self.lock:
                    self.segment_failure_streak = 0
                with self.lock:
                    is_slow = duration >= segment.duration
                    if is_slow:
                        self.slow_segments += 1
                    if segment.sequence not in self.cache:
                        self.last_segment_download = time.time()
                        self.cache[segment.sequence] = (segment.duration, data)
                        self.cache = OrderedDict(sorted(self.cache.items()))
                        self.total_bytes += len(data)
                    self._evict()
                if is_slow:
                    self._notify_event("segment_slow", "SEGMENT_DOWNLOAD_SLOW", round(duration, 2))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.IncompleteRead) as exc:
            self.record_upstream_failure('MANIFEST', exc)
            code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            # Retry transport failures too; invalid HLS parsing should not
            # cause a tight loop of requests to a broken endpoint.
            with self.lock:
                self.manifest_failure_streak += 1
                self.consecutive_manifest_404 = (
                    self.consecutive_manifest_404 + 1 if code == 404 else 0
                )
                streak = self.manifest_failure_streak
                if streak >= 2:
                    # Keep recovery responsive for live viewers. Long 20-60s
                    # backoffs amplified brief provider outages into freezes.
                    delay = min(6.0, 2.0 * (2 ** min(streak - 2, 2)))
                    self.manifest_backoff_until = time.monotonic() + delay
            # When 404 or repeated 5xx indicates a rotated/expired HLS URL,
            # resolve a fresh channel URL from the newly configured provider M3U.
            # refresh_source has its own 20s cooldown.
            if code == 404 or (code in (500, 502, 503, 504) and streak >= 2):
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
            self.check_stall()
            self.stop.wait(max(0.2, self.poll - (time.monotonic() - start)))

    def _window(self, run: list[tuple[int, float]], end: int, commit: bool) -> list[tuple[int, float]]:
        start = max(0, end - 7)
        if self._published_start is not None:
            # A late download can extend this contiguous run to the left.
            start = max(start, self._published_start - run[0][0])
        if commit:
            self._published_start = run[start][0]
        return run[start:end + 1]

    def _paced_edge(self, run: list[tuple[int, float]], last: int, now: float) -> tuple[int, float]:
        """Advance from the last published segment using actual segment durations."""
        index = last - run[0][0]
        due = self._last_published_at if self._last_published_at is not None else now
        # A new segment can be published after the currently published
        # segment's EXTINF duration has elapsed. Using the *next* segment's
        # duration delays playback on variable-duration HLS streams.
        while index + 1 < len(run) and due + run[index][1] <= now:
            due += run[index][1]
            index += 1
        return index, due if index < len(run) - 1 else now

    def published(self, commit: bool = True) -> list[tuple[int, float]]:
        """Publish a monotonic HLS window, including through a missing segment.

        Preserve the preceding cached run until the new run can supply at
        least three segments. Then follow the new run at a temporarily
        reduced delay instead of freezing on the first three fragments.
        On-demand channels may start with three cached segments after three seconds.
        """
        with self.lock:
            if not self.cache or self.latest is None:
                return []
            # Split the ordered cache at missing sequence numbers.
            runs: list[list[tuple[int, float]]] = []
            for seq, (duration, _) in self.cache.items():
                if not runs or seq != runs[-1][-1][0] + 1:
                    runs.append([])
                runs[-1].append((seq, duration))
            tail = runs[-1]
            last = self.last_published_seq
            now = time.monotonic()
            if last is None:
                reserve = self.startup_reserve
                if self._fast_start_at is not None and now >= self._fast_start_at:
                    reserve = 0
                candidates = reversed(runs) if self._fast_start_at is not None else (tail,)
                tail = next((run for run in candidates if len(run) >= 3
                             and (not reserve or sum(s for _, s in run[3:]) >= reserve)), [])
                if not tail:
                    return []
            if last is not None and tail[-1][0] < last:
                return []  # Never rewind the HLS sequence.

            if last is not None and tail[0][0] > last:
                old = next((run for run in runs[:-1]
                            if run[0][0] <= last <= run[-1][0]), None)
                # Continue serving cached video on the old side of the gap
                # while the new run is still too short to restart playback.
                if old and (old[-1][0] > last or len(tail) < 3):
                    # Do not jump over cached older video. Release at the
                    # advertised segment pace while the new run warms up.
                    end_index, advanced_at = self._paced_edge(old, last, now)
                    if commit and old[end_index][0] > last:
                        self.last_published_seq = old[end_index][0]
                        self._last_published_at = advanced_at
                    return self._window(old, end_index, commit)
                if len(tail) < 3:
                    return []
                # Crossing an upstream gap is a deliberate jump. Retain this
                # mode until a fresh session/sequence reset, otherwise the
                # old 90-second requirement would freeze the next 10 updates.
                if commit:
                    self._recovery_mode = True
                recovery = True
            else:
                recovery = self._recovery_mode

            if len(tail) < 3:
                return []

            advanced_at = now
            if recovery:
                # Keep a little headroom once four or more segments exist,
                # but always move the published edge when new segments arrive.
                cutoff = len(tail) - 2 if len(tail) >= 4 else 2
            else:
                remaining = 0.0
                cutoff = -1
                for i in range(len(tail) - 1, -1, -1):
                    if remaining >= self.delay:
                        cutoff = i
                        break
                    remaining += tail[i][1]
                if last is None:
                    cutoff = max(cutoff, 2)
                if last is not None and self._last_published_at is not None:
                    # The source can refill in bursts. Keep serving at playback
                    # speed even while the reserve is below the preferred delay.
                    paced, due = self._paced_edge(tail, last, now)
                    if paced >= cutoff:
                        cutoff = max(2, paced)
                        advanced_at = due if paced >= 2 else now
                if cutoff < 2:
                    return []

            if last is not None:
                forward_index = next((i for i, (seq, _) in enumerate(tail)
                                      if seq >= last), None)
                if forward_index is not None:
                    cutoff = max(cutoff, forward_index)
            if commit:
                if self.last_published_seq != tail[cutoff][0]:
                    self._last_published_at = advanced_at
                self.last_published_seq = tail[cutoff][0]
            return self._window(tail, cutoff, commit)

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

    def segment(self, seq: int, *, record_miss: bool = False) -> bytes | None:
        with self.lock:
            found = self.cache.get(seq)
            if found is None and record_miss:
                self.record_client('segment', 404)
                self.recent_client_segment_404.append(dict(
                    utc_unix=round(self.last_client_segment_access, 1), sequence=seq,
                    cache_first=next(iter(self.cache), None),
                    cache_last=next(reversed(self.cache), None)))
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

    def source_diagnostics(self) -> dict:
        """Capture only playback metrics, without formatting the admin status."""
        with self.lock:
            playable = self.published(commit=False)
            newest = next(reversed(self.cache), None)
            tail_seconds = 0.0
            tail_segments = 0
            seq = newest
            while seq in self.cache:
                tail_seconds += self.cache[seq][0]
                tail_segments += 1
                seq -= 1
            behind_seconds = None
            if playable:
                seq = playable[-1][0] + 1
                seconds = 0.0
                while seq in self.cache:
                    seconds += self.cache[seq][0]
                    seq += 1
                behind_seconds = round(seconds, 1)
            state = ('READY' if playable else 'WAITING_FOR_VIDEO' if not self.cache
                     else 'WARMING_OR_REBUILDING_BUFFER')
            now = time.time()
            return dict(buffer_state=state, contiguous_tail_segments=tail_segments,
                        contiguous_tail_seconds=round(tail_seconds, 1),
                        effective_buffer_delay_seconds=behind_seconds,
                        last_published_sequence=self.last_published_seq,
                        seconds_since_published_advance=round(
                            max(0.0, time.monotonic() - self._last_published_at), 1
                        ) if self._last_published_at is not None else None,
                        last_downloaded_sequence=newest,
                        seconds_since_segment_download=round(now - self.last_segment_download, 1)
                        if self.last_segment_download else None,
                        seconds_since_new_segment=round(now - self.last_progress, 1)
                        if self.last_progress else None)

    def status(self) -> bytes:
        with self.lock:
            playback = self.source_diagnostics()
            info = dict(**playback, ready=playback["buffer_state"] == "READY", delay_seconds=self.delay,
                        startup_reserve_seconds=self.startup_reserve,
                        recovery_mode=self._recovery_mode,
                        latest_upstream_sequence=self.latest,
                        client_manifest_503=self.client_manifest_503,
                        client_segment_404=self.client_segment_404,
                        recent_client_segment_404=list(self.recent_client_segment_404),
                        client_playlist_200=self.client_playlist_200,
                        client_manifest_200=self.client_manifest_200,
                        client_segment_200=self.client_segment_200,
                        client_segment_206=self.client_segment_206,
                        client_auth_404=self.client_auth_404,
                        seconds_since_client_playlist=round(time.time()-self.last_client_playlist_access, 1) if self.last_client_playlist_access else None,
                        seconds_since_client_manifest=round(time.time()-self.last_client_manifest_access, 1) if self.last_client_manifest_access else None,
                        seconds_since_client_segment=round(time.time()-self.last_client_segment_access, 1) if self.last_client_segment_access else None,
                        seconds_since_client_auth_failure=round(time.time()-self.last_client_auth_failure, 1) if self.last_client_auth_failure else None,
                        cached_segments=len(self.cache), cached_mib=round(self.total_bytes / 1048576, 2),
                        errors=self.errors, slow_segments=self.slow_segments,
                        last_error=self.last_error,
                        last_upstream_failure=self.last_upstream_failure,
                        seconds_since_last_upstream_failure=round(
                            time.time() - self.last_upstream_failure_at, 1
                        ) if self.last_upstream_failure_at is not None else None,
                        error_breakdown=dict(self.error_breakdown),
                        recent_upstream_failures=list(self.recent_upstream_failures),
                        consecutive_manifest_404=self.consecutive_manifest_404,
                        manifest_failure_streak=self.manifest_failure_streak,
                        segment_failure_streak=self.segment_failure_streak,
                        manifest_retry_in_seconds=round(
                            max(0.0, self.manifest_backoff_until - time.monotonic()), 1),
                        source_refresh_attempts=self.source_refresh_attempts,
                        source_refresh_errors=self.source_refresh_errors,
                        source_refresh_error=self.source_refresh_error,
                        source_changes=self.source_changes,
                        seconds_since_source_change=round(time.time() - self.last_source_change, 1) if self.last_source_change else None,
                        seconds_since_playlist_update=round(time.time() - self.last_update, 1) if self.last_update else None,
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
                data = gateway.segment(seq, record_miss=True)
                if data is None:
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
