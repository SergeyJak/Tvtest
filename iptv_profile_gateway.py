"""Live profile router sharing the existing IPTV HTTP port and Caddy route.

The legacy slot retains its old token and worker while it uses the local file.
Other profiles are loaded from owner-only snapshots and routed by distinct
random path tokens. Saving a profile swaps only that profile's manager.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from iptv_profiles import ProfileStore


class ProfileRegistry:
    def __init__(self, store: ProfileStore, legacy_manager, legacy_token: str,
                 legacy_epg: Path | None, manager_options: dict):
        self.store = store
        self.legacy_manager = legacy_manager
        self.legacy_token = legacy_token
        self.legacy_epg = legacy_epg
        self.options = dict(manager_options)
        self.lock = threading.RLock()
        self.last_mtime = -1
        self.managers = {}  # slot -> (revision, manager)
        self.routes = {}
        self.closed = False
        self.legacy_detached = False
        self._reload()

    def _reload(self):
        # Check the small config file, not the 2MB channel snapshots or EPG.
        try:
            stat = self.store.path.stat()
            key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            return
        with self.lock:
            if self.closed or key == self.last_mtime:
                return
            self._reload_routes()
            self.last_mtime = key

    def _reload_routes(self):
        from iptv_multi import MultiGateway, channels_from_text

        # The writer replaces the snapshot and profiles.json under this lock.
        with self.store._locked():
            rows = self.store.all()
            routes = {}
            live_slots = set()
            for row in rows:
                slot, token = row["slot"], row["token"]
                if slot == 1 and not row["source_url"]:
                    routes[token] = (self.legacy_manager, token, self.legacy_epg)
                    continue
                if not self.store.configured(row):
                    continue
                snapshot = self.store.media_path(row)
                channels = channels_from_text(snapshot.read_text(encoding="utf-8-sig"))
                if not channels:
                    continue
                live_slots.add(slot)
                if slot == 1 and not self.legacy_detached:
                    # The original manager may be pinned/prewarmed. Release it
                    # when the original token moves to the new source.
                    self.legacy_manager.close()
                    self.legacy_detached = True
                cached = self.managers.get(slot)
                if cached and cached[0] == row["revision"]:
                    manager = cached[1]
                    # A scheduled refresh may rotate URLs without changing
                    # channel IDs. New workers use the updated catalog.
                    with manager.lock:
                        manager.channels = {channel.id: channel for channel in channels}
                else:
                    opts = dict(self.options)
                    # Keep original primary capacity after switching its M3U
                    # from the local file to a provider URL. A one-stream cap
                    # can block channel changes until the old stream expires.
                    opts["max_streams"] = self.options["max_streams"] if slot == 1 else 1
                    opts["telemetry"] = self.options.get("telemetry")
                    opts["profile_slot"] = slot
                    manager = MultiGateway(channels, snapshot, row["source_url"], **opts)
                    self.managers[slot] = (row["revision"], manager)
                    thread = threading.Thread(target=manager.cleanup_loop,
                                              daemon=True, name=f"profile-{slot}-sweep")
                    thread.start()
                    if cached:
                        cached[1].close()
                guide = self.store.guide_path(row)
                if not self.store.guide_source(row):
                    guide = None
                routes[token] = (manager, token, guide)
            for slot in list(self.managers):
                if slot not in live_slots:
                    self.managers.pop(slot)[1].close()
            self.routes = routes

    def resolve(self, token: str):
        self._reload()
        with self.lock:
            return self.routes.get(token)

    def close(self):
        with self.lock:
            self.closed = True
            for _, manager in self.managers.values():
                manager.close()
            self.managers.clear()
            self.routes.clear()
