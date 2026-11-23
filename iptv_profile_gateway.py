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
from iptv_cdn_failover import FailoverMux, is_pilot


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
        self.failovers = {}  # (home cabin, channel ID) => isolated virtual HLS timeline
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

    def _slot_for_token(self, token: str) -> int | None:
        return next((row["slot"] for row in self.store.all()
                     if row["token"] == token), None)

    def failover_for(self, token: str, cid: str):
        """All three TV cabinets prefer their own CDN for the pilot channel.

        Cross-cabinet sources are background-only and never preempt viewers
        watching a different channel in their own account. No provider URL
        or another user's playback token is exposed to the TV.
        """
        if self.closed or (self.store.directory / "multicdn.disabled").exists():
            return None
        with self.lock:
            home = self._slot_for_token(token)
            owner = self.routes.get(token)
            if home is None or owner is None or cid not in owner[0].channels:
                return None
            if not is_pilot(owner[0].channels[cid].name):
                return None
            candidates = []
            for slot in (home, *(other for other in (1, 2, 3) if other != home)):
                profile = self.store.get(slot)
                route = self.routes.get(profile["token"])
                if not route:
                    continue
                manager = route[0]
                matching_id = next(
                    (channel.id for channel in manager.channels.values()
                     if is_pilot(channel.name)), None)
                if matching_id is None:
                    continue
                stream = manager.start(matching_id, standby=(slot != home))
                if stream is not None:
                    candidates.append((slot, stream))
            # Virtual IDs are used from the FIRST request in every cabin:
            # adding a reserve later cannot invalidate TV's old segment URLs.
            mux = self.failovers.setdefault((home, cid), FailoverMux(home_slot=home))
            return mux, candidates

    def publish_failover(self, mux: FailoverMux, token: str,
                         candidates: list[tuple[int, object]]) -> bytes | None:
        """Serialize CDN placement decisions across users for stable capacity.

        The usage snapshot never counts inactive TV sessions. A healthy
        home CDN always takes priority; fallback source has a two-TV cap.
        """
        with self.lock:
            now = time.monotonic()
            usage: dict[int, int] = {}
            for other in self.failovers.values():
                if other is mux:
                    continue
                with other.lock:
                    slot = other.source_slot
                    live = now - other.last_request < 45.0 and other.history
                if slot is not None and live:
                    usage[slot] = usage.get(slot, 0) + 1
            return mux.playlist(token, candidates, now=now, usage=usage)

    def existing_failover(self, token: str, cid: str):
        """Return only the calling TV's private virtual HLS timeline."""
        if (self.store.directory / "multicdn.disabled").exists():
            return None
        with self.lock:
            slot = self._slot_for_token(token)
            return self.failovers.get((slot, cid)) if slot is not None else None

    def failover_status(self, token: str):
        with self.lock:
            home = self._slot_for_token(token)
            if home is None:
                return None
            names = {row["slot"]: row["name"] for row in self.store.all()}
            result = []
            for (owner_slot, _), mux in self.failovers.items():
                if owner_slot != home:
                    continue
                status = mux.status()
                status["home_name"] = names.get(home, "Кабинет")
                for source in status["sources"]:
                    source["name"] = names.get(source["cabinet"], "Кабинет")
                result.append(status)
            return result

    def pilot_diagnostics(self, token: str):
        """Safe explanation of why the VF multi-CDN mux is not active.

        Read already loaded catalogues and workers, never fetch remote M3U,
        start a stream, or return provider URLs, tokens, IPs or viewer data.
        """
        with self.lock:
            home = self._slot_for_token(token)
            if home is None:
                return None
            disabled = (self.store.directory / "multicdn.disabled").exists()
            cabins = []
            possible_names = []
            for slot in (1, 2, 3):
                profile = self.store.get(slot)
                route = self.routes.get(profile["token"])
                manager = route[0] if route else None
                if manager is None:
                    cabins.append({"slot": slot, "name": profile["name"],
                                   "configured": False, "channel_found": False,
                                   "playing": False})
                    continue
                matched = next((channel for channel in manager.channels.values()
                                if is_pilot(channel.name)), None)
                if slot == home and not matched:
                    possible_names = [
                        channel.name[:100] for channel in manager.channels.values()
                        if "теория" in channel.name.casefold()
                        and "взрыва" in channel.name.casefold()
                    ][:4]
                stream = None
                if matched:
                    with manager.lock:
                        stream = manager.active.get(matched.id)
                cabins.append({
                    "slot": slot,
                    "name": profile["name"],
                    "configured": True,
                    "channel_found": matched is not None,
                    "channel_name": matched.name if matched else None,
                    "playing": bool(stream and not stream.standby),
                })
            mux_active = any(owner_slot == home for owner_slot, _ in self.failovers)
            return {
                "enabled": not disabled,
                "home_cabinet": home,
                "pilot_active": mux_active,
                "primary_channel_found": cabins[home - 1]["channel_found"],
                "possible_names": possible_names,
                "cabins": cabins,
            }

    def close(self):
        with self.lock:
            self.closed = True
            for _, manager in self.managers.values():
                manager.close()
            self.managers.clear()
            self.routes.clear()
