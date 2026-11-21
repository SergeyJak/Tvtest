#!/usr/bin/env python3
"""Repeated HLS stall sampling. Private stream URLs are never written to reports."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from iptv_healthcheck import choose, entries_from_m3u, run


def safe_cell(value):
    """Prevent spreadsheet formula injection from channel names."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def summarize(rows):
    per_channel = defaultdict(list)
    for row in rows:
        per_channel[row["channel"]].append(row)
    print("\nChannel                     OK/total   p95 first video byte (ms)   sampled Mbps avg")
    print("-" * 91)
    for name, samples in per_channel.items():
        ok = [x for x in samples if x["status"] == "OK"]
        latencies = [int(x["segment_ttfb_ms"]) for x in ok if x["segment_ttfb_ms"] != ""]
        rates = [float(x["download_mbps"]) for x in ok if x["download_mbps"] != ""]
        p95 = sorted(latencies)[max(0, int((len(latencies) - 1) * 0.95))] if latencies else "-"
        avg = round(statistics.mean(rates), 2) if rates else "-"
        print(f"{name[:27]:27} {len(ok):>3}/{len(samples):<5} {str(p95):>24} {str(avg):>18}")
    errors = Counter(x["status"] for x in rows if x["status"] != "OK")
    print("Errors:", dict(errors) if errors else "none")
    print("Note: this samples only the beginning of one segment per round. It cannot by itself prove a healthy, continuously playable feed.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Track intermittent IPTV playlist/segment slowdowns")
    ap.add_argument("--playlist", required=True, type=Path, help="Private local M3U playlist path")
    ap.add_argument("--channel", action="append", default=[], help="Channel substring; repeatable")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--interval", type=float, default=15, help="Minimum seconds between sample starts")
    ap.add_argument("--timeout", type=float, default=8)
    ap.add_argument("--probe-mib", type=float, default=2, help="Download cap per channel and round")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path("iptv_health_report.csv"))
    ap.add_argument("--dry-run", action="store_true", help="Preview channels without network requests")
    args = ap.parse_args(argv)
    if min(args.limit, args.rounds, args.interval, args.timeout, args.probe_mib, args.workers) <= 0:
        ap.error("limit, rounds, interval, timeout, probe-mib and workers must be > 0")
    try:
        channels = entries_from_m3u(args.playlist)
    except (OSError, UnicodeError):
        ap.error("Cannot read playlist file")
    selected = choose(channels, args.channel, args.limit)
    if not selected:
        print("No matching channels")
        return 1
    print(f"Playlist contains {len(channels)} channels. Checking {len(selected)}; URLs hidden.")
    if args.dry_run:
        for name, group, _ in selected:
            print(f"  {name} [{group}]")
        return 0
    rows = []
    for index in range(args.rounds):
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(run, entry, args.timeout, int(args.probe_mib * 1024 * 1024)) for entry in selected]
            samples = [future.result() for future in futures]
        for sample in samples:
            sample["utc_time"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            sample["round"] = index + 1
        rows.extend(samples)
        statuses = Counter(s["status"] for s in samples)
        print(f"Round {index + 1}/{args.rounds}: " + ", ".join(f"{status}={count}" for status, count in statuses.items()), flush=True)
        if index != args.rounds - 1:
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    with args.out.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({key: safe_cell(value) for key, value in row.items()} for row in rows)
    summarize(rows)
    print(f"Saved {len(rows)} measurements to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
