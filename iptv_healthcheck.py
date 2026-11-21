#!/usr/bin/env python3
"""Local HLS/IPTV diagnostics. No external Python dependencies.

Example:
  python iptv_healthcheck.py forgpt.m3u8 --limit 10
  python iptv_healthcheck.py forgpt.m3u8 --channel "Первый канал" --channel "НТВ"

Only reports names, groups and timings. Stream URLs/tokens are never printed or saved.\nUse iptv_probe.py for repeated monitoring.
"""
from __future__ import annotations
import argparse
import csv
import io
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 IPTV-Diagnostics/1.0"
PREF = ["Первый канал", "Россия 1", "НТВ", "РЕН ТВ", "Матч ТВ", "РБК", "Россия 24", "Discovery", "BBC One", "Eurosport", "National Geographic", "Карусель", "СТС", "ТНТ"]


def entries_from_m3u(path: Path):
    out = []
    name, group = None, ""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if s.startswith("#EXTINF:"):
            name = s.rsplit(",", 1)[-1].strip()
            group = ""
        elif s.startswith("#EXTGRP:"):
            group = s[len("#EXTGRP:"):].strip()
        elif s and not s.startswith("#") and name is not None:
            out.append((name, group, s))
            name = None
    return out


def choose(entries, patterns, limit):
    if patterns:
        found = [e for e in entries if any(p.lower() in e[0].lower() for p in patterns)]
        return found[:limit]
    result = []
    used = set()
    for word in PREF:
        candidates = [e for e in entries if word.lower() in e[0].lower() and e[2] not in used]
        if candidates:
            # Prefer the actual main channel (e.g. НТВ over НТВ Хит), HD over UHD.
            def score(e):
                suffix = e[0][len(word):].strip() if e[0].lower().startswith(word.lower()) else e[0]
                is_main = suffix.lower() in {"", "hd", "hd orig", "hd 50", "fhd", "sd", "uhd", "4k"}
                is_hd = suffix.lower() in {"hd", "hd orig", "hd 50", "fhd"}
                return (not is_main, not is_hd, "UHD" in e[0] or "4K" in e[0], "+" in e[0] or "(" in e[0])
            candidates.sort(key=score)
            e = candidates[0]
            result.append(e)
            used.add(e[2])
        if len(result) == limit:
            break
    return result or entries[:limit]


def get_text(url, timeout, maxbytes=1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        first = resp.read(1)
        first_byte_ms = int((time.perf_counter() - start) * 1000)
        body = first + resp.read(maxbytes)
    return body.decode("utf-8-sig", "replace"), first_byte_ms


def hls_next_url(manifest, base, master=False):
    lines = [s.strip() for s in manifest.splitlines()]
    for i, line in enumerate(lines):
        if master and line.startswith("#EXT-X-STREAM-INF"):
            for nextline in lines[i + 1:]:
                if nextline and not nextline.startswith("#"):
                    return urllib.parse.urljoin(base, nextline)
        elif not master and line.startswith("#EXTINF"):
            for nextline in lines[i + 1:]:
                if nextline and not nextline.startswith("#"):
                    return urllib.parse.urljoin(base, nextline)
    if not master:
        # Some HLS playlists list segments without EXTINF.
        for line in lines:
            if line and not line.startswith("#"):
                return urllib.parse.urljoin(base, line)
    return None


def segment_probe(url, timeout, maxbytes):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    begin = time.perf_counter()
    n = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        first = resp.read(1)
        ttfb = time.perf_counter() - begin
        n += len(first)
        while n < maxbytes:
            chunk = resp.read(min(65536, maxbytes - n))
            if not chunk:
                break
            n += len(chunk)
    elapsed = max(time.perf_counter() - begin, 1e-6)
    return round(ttfb * 1000), round(n * 8 / elapsed / 1_000_000, 2), round(n / 1024 / 1024, 2)


def run(entry, timeout, probe_bytes):
    name, group, url = entry
    rec = {"channel": name, "group": group, "manifest_ttfb_ms": "", "segment_ttfb_ms": "", "download_mbps": "", "download_mib": "", "status": ""}
    try:
        manifest, ttfb = get_text(url, timeout)
        rec["manifest_ttfb_ms"] = ttfb
        if "#EXTM3U" not in manifest:
            rec["status"] = "Not an HLS manifest (server response?)"
            return rec
        if "#EXT-X-STREAM-INF" in manifest:
            variant = hls_next_url(manifest, url, master=True)
            if not variant:
                raise ValueError("Master playlist without variant")
            manifest, _ = get_text(variant, timeout)
            url = variant
        segment = hls_next_url(manifest, url)
        if not segment:
            raise ValueError("No media segment in playlist")
        ms, mbps, mib = segment_probe(segment, timeout, probe_bytes)
        rec.update(segment_ttfb_ms=ms, download_mbps=mbps, download_mib=mib, status="OK")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        rec["status"] = f"HTTP_{exc.code}" if isinstance(exc, urllib.error.HTTPError) else ("TIMEOUT" if isinstance(exc, TimeoutError) else f"ERROR: {type(exc).__name__}")
    return rec


def main():
    ap = argparse.ArgumentParser(description="Measure HLS manifest/segment startup and download speed for IPTV playlist channels")
    ap.add_argument("playlist", nargs="?", default="forgpt.m3u8")
    ap.add_argument("--channel", action="append", help="Substring to find channel (repeatable)")
    ap.add_argument("--limit", type=int, default=10, help="Number of channels (default 10)")
    ap.add_argument("--timeout", type=float, default=8, help="Per-request timeout in seconds (default 8)")
    ap.add_argument("--probe-mib", type=float, default=2, help="Maximum downloaded media MB per channel (default 2 MiB)")
    ap.add_argument("--dry-run", action="store_true", help="Preview selections without fetching stream")
    ap.add_argument("--out", default="iptv_health_report.csv", help="Output CSV filename")
    args = ap.parse_args()
    if args.limit <= 0 or args.timeout <= 0 or args.probe_mib <= 0:
        ap.error("limit, timeout and probe-mib must be > 0")
    try:
        entries = entries_from_m3u(Path(args.playlist))
    except (OSError, UnicodeError) as ex:
        ap.error(str(ex))
    selected = choose(entries, args.channel, args.limit)
    if not selected:
        print("No matching channels", file=sys.stderr)
        return 1
    print(f"Playlist: {len(entries)} channels; checking {len(selected)} channels. URLs are hidden.")
    if args.dry_run:
        for name, group, _ in selected:
            print(f"  {name} [{group}]")
        return 0
    rows = []
    for ix, entry in enumerate(selected, 1):
        print(f"[{ix}/{len(selected)}] {entry[0]} ... ", end="", flush=True)
        row = run(entry, args.timeout, int(args.probe_mib * 1024 * 1024))
        rows.append(row)
        print(f"{row['status']}, manifest {row['manifest_ttfb_ms']} ms, segment {row['segment_ttfb_ms']} ms, {row['download_mbps']} Mbps")
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    print(f"Saved: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())