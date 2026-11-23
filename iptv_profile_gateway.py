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
from iptv_cdn_failover import FailoverMux, SESSION_IDLE_SECONDS


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

    @staticmethod
    def _canonical_name(name: str) -> str:
        return " ".join(name.casefold().split())

    @classmethod
    def _matching_channel_id(cls, channels: dict, wanted) -> str | None:
        """Match the exact channel, never a similar-but-different programme.

        Exact stable IDs handle duplicate channel names and M3U catalogue
        ordering. If IDs differ across CDN playlists, use a *unique* channel
        name; reject ambiguous duplicates rather than playing wrong content.
        """
        same_id = channels.get(wanted.id)
        if same_id is not None:
            if cls._canonical_name(same_id.name) == cls._canonical_name(wanted.name):
                return wanted.id
            return None
        name = cls._canonical_name(wanted.name)
        matches = [channel for channel in channels.values()
                   if cls._canonical_name(channel.name) == name]
        if len(matches) != 1:
            return None
        candidate = matches[0]
        if (wanted.group and candidate.group and
                cls._canonical_name(wanted.group) != cls._canonical_name(candidate.group)):
            return None
        return candidate.id

    def _prune_failovers(self, now: float):
        """Release old channel buffers after its TV session ends."""
        for key, mux in list(self.failovers.items()):
            if now - mux.last_request > 70:
                del self.failovers[key]

    def failover_for(self, token: str, cid: str):
        """Use every channel's HOME Edem subscription with optional backups.

        Standby streams are started on demand for the *same* channel only;
        they never evict an active viewer of a different channel. A channel
        missing in a backup stays playable from its owner's own source.
        """
        if self.closed or (self.store.directory / "multicdn.disabled").exists():
            return None
        with self.lock:
            home = self._slot_for_token(token)
            owner = self.routes.get(token)
            if home is None or owner is None or cid not in owner[0].channels:
                return None
            wanted = owner[0].channels[cid]
            self._prune_failovers(time.monotonic())
            candidates = []
            for slot in (home, *(other for other in (1, 2, 3) if other != home)):
                profile = self.store.get(slot)
                route = self.routes.get(profile["token"])
                if not route:
                    continue
                manager = route[0]
                matching_id = (cid if slot == home else
                               self._matching_channel_id(manager.channels, wanted))
                if matching_id is None:
                    continue
                worker = manager.start(matching_id, standby=(slot != home))
                if worker is not None:
                    candidates.append((slot, worker))
            # Every channel in each TV cabin starts with virtual numbering,
            # even when both backups are missing or busy.
            mux = self.failovers.setdefault(
                (home, cid), FailoverMux(home_slot=home,
                                         channel_id=cid, channel_name=wanted.name))
            return mux, candidates

    def publish_failover(self, mux: FailoverMux, token: str,
                         candidates: list[tuple[int, object]]) -> bytes | None:
        """Serialize CDN placement decisions across users for stable capacity.

        The usage snapshot never counts inactive TV sessions. A healthy
        home CDN always takes priority; each source may carry only one
        guest TV, preserving space for its owner's own playback.
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
                if slot is not None and live and slot != other.home_slot:
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
            self._prune_failovers(time.monotonic())
            for (owner_slot, _), mux in self.failovers.items():
                if owner_slot != home:
                    continue
                status = mux.status()
                status["home_name"] = names.get(home, "Кабинет")
                # The bearer TV token must not reveal other family members'
                # cabinet names. Authenticated admin enriches names locally.
                for source in status["sources"]:
                    if source["cabinet"] == home:
                        source["name"] = names.get(home, "Кабинет")
                result.append(status)
            return result

    def pilot_diagnostics(self, token: str):
        """Read-only status for all-channel multi-CDN, without creating streams.

        The method name is kept for compatibility with the gateway status
        route. Public TV tokens reveal only their own viewing activity.
        """
        with self.lock:
            home = self._slot_for_token(token)
            if home is None:
                return None
            disabled = (self.store.directory / "multicdn.disabled").exists()
            recent = [
                (mux.last_request, cid, mux)
                for (slot, cid), mux in self.failovers.items() if slot == home
            ]
            selected = max(recent, default=None)
            channel_id = selected[1] if selected is not None else None
            owner = self.routes.get(token)
            wanted = owner[0].channels.get(channel_id) if owner and channel_id else None
            cabins = []
            for slot in (1, 2, 3):
                profile = self.store.get(slot)
                route = self.routes.get(profile["token"])
                manager = route[0] if route else None
                match = (self._matching_channel_id(manager.channels, wanted)
                         if manager and wanted else None)
                cabin = {
                    "slot": slot,
                    "name": profile["name"] if slot == home else f"Кабинет {slot}",
                    "configured": manager is not None,
                    "channel_found": match is not None if wanted else bool(manager and manager.channels),
                    "channel_name": wanted.name if slot == home and wanted else None,
                }
                if slot == home:
                    cabin["playing"] = False
                    if manager is not None:
                        with manager.lock:
                            cabin["playing"] = any(
                                not stream.standby for stream in manager.active.values())
                cabins.append(cabin)
            return {
                "enabled": not disabled,
                "home_cabinet": home,
                "pilot_active": bool(recent),
                "primary_channel_found": bool(owner and owner[0].channels),
                "selected_channel": wanted.name if wanted else None,
                "possible_names": [],
                "cabins": cabins,
            }

    def close(self):
        with self.lock:
            self.closed = True
            for _, manager in self.managers.values():
                manager.close()
            self.managers.clear()
            self.routes.clear()
