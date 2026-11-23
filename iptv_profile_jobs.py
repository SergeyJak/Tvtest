#!/usr/bin/env python3
"""Refresh configured profile snapshots and produce separate XMLTV EPG guides.

No provider credentials or URLs are ever printed. Single generation lock on VPS.
M3U updates and EPG builds are atomic. EPG uses the correct channel IDs for
each profile, never the EPG from somebody else's subscription.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from iptv_profiles import ProfileStore, ProfileError, download_playlist, validate_source_url
from iptv_gateway import safe_error


def generate_for_slot(store: ProfileStore, slot: int, *, refresh_m3u: bool = False,
                      m3u_only: bool = False) -> str:
    from iptv_multi import channels_from_text
    from iptv_epg_stream import build_epg

    row = store.get(slot)
    if not row["source_url"]:
        return "legacy_or_unconfigured"
    source_path = store.snapshot_path(slot)
    if refresh_m3u:
        updated = download_playlist(row["source_url"])
        ids = [channel.id for channel in channels_from_text(updated)]
        if not ids or len(ids) != len(set(ids)):
            raise ProfileError("Получен пустой или некорректный каталог")
        with store._locked():
            latest = store._read()
            record = latest["profiles"][slot - 1]
            if record["revision"] != row["revision"]:
                return "changed_while_refreshing"
            current_ids = [c.id for c in channels_from_text(source_path.read_text(encoding="utf-8-sig"))]
            if ids != current_ids:
                record["revision"] += 1
                # Keep the last good EPG until a newly generated guide
                # is atomically installed. M3U-only refresh must never
                # remove a working TV guide.
            from iptv_profiles import atomic_private_write
            atomic_private_write(source_path, updated)
            atomic_private_write(store.path, json.dumps(latest, ensure_ascii=False))
            row = dict(record)
    if m3u_only:
        return "refreshed_m3u"
    guide_source = store.guide_source(row)
    if not guide_source:
        return "no_epg_source"
    validate_source_url(guide_source)
    playlist = source_path.read_text(encoding="utf-8-sig")
    fd, temp_name = tempfile.mkstemp(prefix=f".epg-{slot}-", suffix=".gz", dir=store.directory)
    os.close(fd)
    candidate = Path(temp_name)
    try:
        # Streaming generator writes output atomically, with external feed
        # access restricted to explicitly validated source URL.
        build_epg(playlist, [guide_source], candidate, {})
        if store.get(slot)["revision"] != row["revision"]:
            return "changed_during_epg_generation"
        os.replace(candidate, store.epg_path(slot))
        return "updated"
    finally:
        candidate.unlink(missing_ok=True)


def run_update(store: ProfileStore, slots: list[int], *, refresh_m3u: bool = False,
               m3u_only: bool = False) -> dict:
    results = {}
    lock_path = store.directory / "epg-update.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for slot in slots:
            try:
                results[slot] = generate_for_slot(store, slot, refresh_m3u=refresh_m3u,
                                                  m3u_only=m3u_only)
            except Exception:
                # Keep old guide when sources are temporarily down; never
                # include URLs, upstream HTTP exceptions or credentials in logs.
                from iptv_profiles import atomic_private_write
                if not m3u_only:
                    atomic_private_write(store.epg_error_path(slot), "EPG_GENERATION_FAILED\n")
                results[slot] = "failed"
            else:
                if results[slot] == "updated":
                    store.epg_error_path(slot).unlink(missing_ok=True)
    return results


def start_generation(store: ProfileStore, slot: int) -> None:
    """Detached generation survives HTTP request completion, never blocks login."""
    script = Path(__file__).resolve()
    try:
        subprocess.Popen([
            sys.executable, str(script), "--slot", str(slot),
            "--profiles-dir", str(store.directory),
            "--legacy-playlist", str(store.legacy_playlist),
            "--legacy-token-file", str(store.legacy_token_file),
            "--legacy-epg", str(store.legacy_epg or ""),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
    except OSError:
        pass  # Scheduled refresh retries when the EPG timer runs.


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profiles-dir", type=Path, default=Path(".iptv_profiles"))
    p.add_argument("--legacy-playlist", type=Path, default=Path("forgpt.m3u8"))
    p.add_argument("--legacy-token-file", type=Path, default=Path(".iptv_gateway_token"))
    p.add_argument("--legacy-epg", type=Path, default=Path("epg.xml.gz"))
    p.add_argument("--slot", type=int, choices=(1, 2, 3))
    p.add_argument("--refresh-m3u", action="store_true")
    p.add_argument("--m3u-only", action="store_true",
                   help="Refresh source URLs without downloading or rebuilding XMLTV EPG")
    args = p.parse_args()
    if args.m3u_only and not args.refresh_m3u:
        p.error("--m3u-only requires --refresh-m3u")
    store = ProfileStore(args.profiles_dir, args.legacy_playlist,
                         args.legacy_epg, args.legacy_token_file)
    result = run_update(store, [args.slot] if args.slot else [1, 2, 3],
                        refresh_m3u=args.refresh_m3u, m3u_only=args.m3u_only)
    # Output contains only numeric slot IDs and generic status codes.
    print(json.dumps(result))
    return 1 if "failed" in result.values() else 0


if __name__ == "__main__":
    raise SystemExit(main())
