#!/usr/bin/env python3
"""Observe a live HLS channel long enough to diagnose intermittent freezes.

Only channel names, sequence numbers and timings are written. URLs are never
printed or included in the report. Downloads full segments up to a safety cap.
No third-party dependencies; Python 3.10+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from iptv_healthcheck import UA, choose, entries_from_m3u, get_text, hls_next_url


@dataclass(frozen=True)
class Segment:
    sequence: int
    duration: float
    url: str


@dataclass(frozen=True)
class Media:
    target_duration: float
    segments: tuple[Segment, ...]
    ended: bool


def parse_media(body: str, base_url: str) -> Media:
    """Read ordinary MPEG-TS/fMP4 live-media playlists (not master playlists)."""
    if not body.lstrip('\ufeff').startswith('#EXTM3U') or '#EXT-X-STREAM-INF' in body:
        raise ValueError('invalid_media_playlist')
    sequence = 0
    target = 6.0
    pending_duration = None
    segments = []
    ended = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-TARGETDURATION:'):
            target = max(1.0, float(line.split(':', 1)[1]))
        elif line.startswith('#EXTINF:'):
            pending_duration = float(line.split(':', 1)[1].split(',', 1)[0])
        elif line == '#EXT-X-ENDLIST':
            ended = True
        elif line and not line.startswith('#') and pending_duration is not None:
            segments.append(Segment(sequence + len(segments), pending_duration,
                                    urllib.parse.urljoin(base_url, line)))
            pending_duration = None
    return Media(target, tuple(segments), ended)


def fetch_media(source_url: str, timeout: float) -> tuple[Media, int]:
    body, ttfb = get_text(source_url, timeout)
    base = source_url
    if '#EXT-X-STREAM-INF' in body:
        variant = hls_next_url(body, source_url, master=True)
        if not variant:
            raise ValueError('missing_variant')
        body, ttfb = get_text(variant, timeout)
        base = variant
    media = parse_media(body, base)
    if not media.segments:
        raise ValueError('empty_media_playlist')
    return media, ttfb


def download_segment(url: str, timeout: float, cap: int) -> tuple[str, int, int, int]:
    """Return (status, bytes, first-byte ms, total ms).

    A capped download is explicitly PARTIAL and cannot prove full throughput.
    """
    started = time.perf_counter()
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    total = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        first = response.read(1)
        ttfb = round((time.perf_counter() - started) * 1000)
        if not first:
            raise ValueError('empty_segment')
        total = len(first)
        while total <= cap:
            chunk = response.read(min(65536, cap + 1 - total))
            if not chunk:
                break
            total += len(chunk)
    elapsed = round((time.perf_counter() - started) * 1000)
    status = 'PARTIAL_CAP' if total > cap else 'OK'
    return status, total, ttfb, elapsed


def safe_status(exc: Exception) -> str:
    """Exception messages can contain subscription tokens, so never return them."""
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP_{exc.code}'
    if isinstance(exc, (TimeoutError,)):
        return 'TIMEOUT'
    if isinstance(exc, urllib.error.URLError):
        return 'NETWORK_ERROR'
    if isinstance(exc, ValueError):
        return 'INVALID_STREAM'
    return type(exc).__name__.upper()


def row(name: str, status: str, seq: int | str = '', duration: float | str = '',
        manifest_ttfb: int | str = '', segment_ttfb: int | str = '',
        download_ms: int | str = '', bytes_read: int | str = '', ratio: float | str = '') -> dict:
    # Prevent spreadsheet formula injection in the only untrusted exported field.
    safe_name = "'" + name if name.startswith(('=', '+', '-', '@', '\t', '\r')) else name
    return {'utc_time': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'channel': safe_name, 'status': status, 'sequence': seq,
            'segment_duration_s': duration, 'manifest_ttfb_ms': manifest_ttfb,
            'segment_ttfb_ms': segment_ttfb, 'download_ms': download_ms,
            'bytes': bytes_read, 'download_to_playback_ratio': ratio}


def observe(entry: tuple[str, str, str], minutes: float, interval: float,
            timeout: float, max_bytes: int, on_row=print) -> list[dict]:
    """Follow successive live segments until the clock expires; skip initial history.

    Staleness is measured from the most recent observed *playlist sequence change*,
    not simply from successful HTTP responses.
    """
    name, _group, url = entry
    deadline = time.monotonic() + minutes * 60
    results = []
    last_seq = None
    latest_seen = None
    last_progress = time.monotonic()
    stale_emitted = False
    while time.monotonic() < deadline:
        tick = time.monotonic()
        try:
            media, manifest_ttfb = fetch_media(url, timeout)
            latest = media.segments[-1].sequence
            if last_seq is None:
                # On live HLS, older segments are already in the past.
                last_seq = latest - 1
            if latest_seen is None or latest > latest_seen:
                latest_seen = latest
                last_progress = time.monotonic()
                stale_emitted = False
            elif not media.ended and not stale_emitted and time.monotonic() - last_progress > max(3 * media.target_duration, 15):
                event = row(name, 'STALE_MANIFEST', latest, manifest_ttfb=manifest_ttfb)
                results.append(event)
                on_row(event)
                stale_emitted = True
            if media.ended:
                event = row(name, 'ENDLIST', latest, manifest_ttfb=manifest_ttfb)
                results.append(event)
                on_row(event)
                break
            pending = [segment for segment in media.segments if segment.sequence > last_seq]
            if pending and pending[0].sequence > last_seq + 1:
                event = row(name, 'MISSED_WINDOW', pending[0].sequence, manifest_ttfb=manifest_ttfb)
                results.append(event)
                on_row(event)
            for segment in pending:
                if time.monotonic() >= deadline:
                    break
                try:
                    status, nbytes, first_ms, elapsed_ms = download_segment(segment.url, timeout, max_bytes)
                    ratio = round(elapsed_ms / (segment.duration * 1000), 2) if segment.duration > 0 and status == 'OK' else ''
                    if ratio != '' and ratio >= 1.0:
                        status = 'SLOW_SEGMENT'
                    event = row(name, status, segment.sequence, segment.duration,
                                manifest_ttfb, first_ms, elapsed_ms, nbytes, ratio)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    event = row(name, 'SEGMENT_' + safe_status(exc), segment.sequence,
                                segment.duration, manifest_ttfb)
                results.append(event)
                on_row(event)
                last_seq = segment.sequence
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            event = row(name, 'MANIFEST_' + safe_status(exc))
            results.append(event)
            on_row(event)
        if time.monotonic() < deadline:
            time.sleep(min(max(0, interval - (time.monotonic() - tick)), max(0, deadline - time.monotonic())))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Monitor continuous HLS playback for stalls')
    parser.add_argument('--playlist', required=True, type=Path)
    parser.add_argument('--channel', action='append', default=[], help='Channel name substring (repeatable)')
    parser.add_argument('--minutes', type=float, default=10, help='Monitoring duration, default 10 minutes')
    parser.add_argument('--interval', type=float, default=2, help='Playlist refresh interval, default 2 seconds')
    parser.add_argument('--timeout', type=float, default=10, help='Per-request network timeout')
    parser.add_argument('--max-segment-mib', type=float, default=32, help='Safety cap per video segment')
    parser.add_argument('--out', type=Path, default=Path('iptv_continuous_report.csv'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if min(args.minutes, args.interval, args.timeout, args.max_segment_mib) <= 0:
        parser.error('All numeric options must be > 0')
    try:
        channels = entries_from_m3u(args.playlist)
    except (OSError, UnicodeError):
        parser.error('Unable to read playlist')
    selected = choose(channels, args.channel, 1)
    if not selected:
        parser.error('No matching channels')
    name = selected[0][0]
    print(f'Monitoring channel: {name}. Stream URLs and tokens hidden.')
    if args.dry_run:
        return 0
    print(f'Duration: {args.minutes:g} min; HLS media segments will be downloaded (data usage may be high).')
    all_rows = []
    def report(event):
        all_rows.append(event)
        if event['status'] != 'OK' or len(all_rows) % 10 == 0:
            print(f"{event['utc_time']} {event['channel']} seq={event['sequence']} {event['status']} ratio={event['download_to_playback_ratio']}", flush=True)
    try:
        observe(selected[0], args.minutes, args.interval, args.timeout,
                int(args.max_segment_mib * 1024 * 1024), report)
    except KeyboardInterrupt:
        print('Interrupted; saving measurements collected so far.')
    if not all_rows:
        print('No HLS measurements captured.')
        return 2
    with args.out.open('w', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=list(all_rows[0]))
        writer.writeheader()
        writer.writerows(all_rows)
    from collections import Counter
    totals = Counter(rec['status'] for rec in all_rows)
    print('Results:', ', '.join(f'{k}={v}' for k, v in sorted(totals.items())))
    print(f'Saved {len(all_rows)} measurements to {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
