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
        self._reload()

    def _reload(self):
        # Check the small config file, not the 2MB channel snapshots or EPG.
        try:
            stat = self.store.path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return
        with self.lock:
            if self.closed or key == self.last_mtime:
                return
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
                live_slots.add(slot)
                cached = self.managers.get(slot)
                if cached and cached[0] == row["revision"]:
                    manager = cached[1]
                else:
                    from iptv_multi import MultiGateway, channels_from_text
                    snapshot = self.store.media_path(row)
                    channels = channels_from_text(snapshot.read_text(encoding="utf-8-sig"))
                    if not channels:
                        continue
                    opts = dict(self.options)
                    opts["max_streams"] = 1
                    opts["telemetry"] = self.options.get("telemetry")
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
            self.last_mtime = key

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
