"""Live profile router sharing the existing IPTV HTTP port and Caddy route.

The legacy slot retains its old token and worker while it uses the local file.
Other profiles are loaded from owner-only snapshots and routed by distinct
random path tokens. Saving a profile swaps only that profile's manager.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from iptv_profiles import ProfileStore
from iptv_cdn_failover import FailoverMux, SESSION_IDLE_SECONDS, MAX_GUESTS_PER_SOURCE

ARCHIVED_CHANNELS_PER_CABIN = 32


@dataclass
class ChannelHistory:
    name: str
    last_request: float
    switches: int
    events: list


@dataclass
class Tuning:
    mux: FailoverMux
    sources: dict = field(default_factory=dict)  # slot -> (manager, worker)

    def candidates(self):
        return [(slot, worker) for slot, (_, worker) in self.sources.items()
                if not worker.gateway.stop.is_set()]


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
        self.tunings: dict[int, Tuning] = {}
        self.history = {slot: {} for slot in (1, 2, 3)}
        self.next_sequences = {}  # Resume numbering after the video cache expires.
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
                        manager.set_channels(channels)
                else:
                    opts = dict(self.options)
                    # Keep original primary capacity after switching its M3U
                    # from the local file to a provider URL. A one-stream cap
                    # can block channel changes until the old stream expires.
                    opts["max_streams"] = self.options["max_streams"] if slot == 1 else 1
                    opts["telemetry"] = self.options.get("telemetry")
                    opts["profile_slot"] = slot
                    opts["prefer_owner_tuning"] = True  # Dedicated TV per Edem cabinet.
                    manager = MultiGateway(channels, snapshot, row["source_url"], **opts)
                    self.managers[slot] = (row["revision"], manager)
                    thread = threading.Thread(target=manager.cleanup_loop,
                                              daemon=True, name=f"profile-{slot}-sweep")
                    thread.start()
                    if cached:
                        cached[1].close()
                    self._end_tuning(slot)
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
    def _matching_channel_id(cls, owner, backup, wanted) -> str | None:
        """An ordinal in one provider's playlist cannot identify a duplicate in another."""
        name = cls._canonical_name(wanted.name)
        home = owner.channels_by_name[name]
        matches = backup.channels_by_name.get(name, [])
        group = cls._canonical_name(wanted.group)
        if len(home) == len(matches) == 1:
            other = cls._canonical_name(matches[0].group)
            return matches[0].id if not group or not other or group == other else None
        if group:
            home = [c for c in home if cls._canonical_name(c.group) == group]
            matches = [c for c in matches if cls._canonical_name(c.group) == group]
            if len(home) == len(matches) == 1:
                return matches[0].id
        return None

    def _release_unused(self, reserves):
        for manager, worker in reserves:
            if any((manager, worker) in tuning.sources.values() for tuning in self.tunings.values()):
                continue
            with manager.lock:
                cid = worker.channel.id
                if (manager.active.get(cid) is worker and worker.standby
                        and not worker.pinned and not worker.pending_requests):
                    worker.close()
                    del manager.active[cid]

    def _end_tuning(self, home):
        previous = self.tunings.pop(home, None)
        if previous:
            self._release_unused(previous.sources.values())
            # Keep already issued segment bytes until expiry, without pinning the upstream cache.
            with previous.mux.lock:
                previous.mux.source_stream = None

    def _prune_failovers(self, now: float):
        """Release old channel buffers after its TV session ends."""
        for home, tuning in list(self.tunings.items()):
            if now - tuning.mux.last_request >= SESSION_IDLE_SECONDS:
                self._end_tuning(home)
        for key, mux in list(self.failovers.items()):
            if now - mux.last_request > 70:
                if mux.switch_events:
                    archive = self.history[mux.home_slot]
                    archive[mux.channel_id] = ChannelHistory(
                        mux.channel_name, mux.last_request, mux.switches, mux.switch_events)
                    if len(archive) > ARCHIVED_CHANNELS_PER_CABIN:
                        del archive[min(archive, key=lambda cid: archive[cid].last_request)]
                self.next_sequences[key] = mux.next_sequence
                del self.failovers[key]

    def failover_for(self, token: str, cid: str):
        """Use every channel's HOME Edem subscription with optional backups.

        Standby streams are started on demand for the *same* channel only;
        they never evict an active viewer of a different channel. A channel
        missing in a backup stays playable from its owner's own source.
        """
        with self.lock:
            if self.closed or (self.store.directory / "multicdn.disabled").exists():
                for slot in list(self.tunings):
                    self._end_tuning(slot)
                return None
            home = self._slot_for_token(token)
            owner = self.routes.get(token)
            if home is None or owner is None or cid not in owner[0].channels:
                return None
            wanted = owner[0].channels[cid]
            self._prune_failovers(time.monotonic())
            worker = owner[0].start(cid)
            if worker is None:
                return None, []
            key = (home, cid)
            tuning = self.tunings.get(home)
            if tuning is None or tuning.mux.channel_id != cid:
                self._end_tuning(home)
                mux = self.failovers.get(key)
                if mux is None:
                    mux = FailoverMux(home_slot=home, channel_id=cid, channel_name=wanted.name,
                                      first_sequence=self.next_sequences.pop(key, 1))
                    history = self.history[home].pop(cid, None)
                    if history:
                        mux.switches = history.switches
                        mux.switch_events = history.events
                    self.failovers[key] = mux
                else:
                    mux.restart_session(time.monotonic())
                tuning = self.tunings[home] = Tuning(mux)
            sources = {home: (owner[0], worker)}
            for profile in self.store.all():
                slot = profile["slot"]
                if slot == home:
                    continue
                route = self.routes.get(profile["token"])
                if not route:
                    continue
                manager = route[0]
                matching_id = self._matching_channel_id(owner[0], manager, wanted)
                if matching_id is None:
                    continue
                worker = manager.start(matching_id, standby=True)
                if worker is not None:
                    sources[slot] = (manager, worker)
            dropped = set(tuning.sources.values()) - set(sources.values())
            tuning.sources = sources
            self._release_unused(dropped)
            return tuning.mux, tuning.candidates()

    def publish_failover(self, mux: FailoverMux, token: str) -> bytes | None:
        """Serialize CDN placement decisions across users for stable capacity.

        The usage snapshot never counts inactive TV sessions. A healthy
        home CDN always takes priority; each source may carry only one
        guest TV, preserving space for its owner's own playback.
        """
        with self.lock:
            now = time.monotonic()
            current = self.tunings.get(mux.home_slot)
            if current is None or current.mux is not mux:
                return None  # An earlier manifest request finished warming after a retune.
            return mux.playlist(token, current.candidates(), now=now, usage=self._guest_usage(mux, now))

    def _guest_usage(self, mux, now):
        usage = {}
        for tuning in self.tunings.values():
            other = tuning.mux
            slot = other.source_slot
            if (other is not mux and other.history and slot not in (None, other.home_slot)
                    and now - other.last_request < SESSION_IDLE_SECONDS):
                usage[slot] = usage.get(slot, 0) + 1
        return usage

    def existing_failover(self, token: str, cid: str):
        """Return only the calling TV's private virtual HLS timeline."""
        if (self.store.directory / "multicdn.disabled").exists():
            return None
        with self.lock:
            self._prune_failovers(time.monotonic())
            slot = self._slot_for_token(token)
            return self.failovers.get((slot, cid)) if slot is not None else None

    def _reports_for_cabin(self, home, name):
        result = []
        tuning = self.tunings.get(home)
        for (owner_slot, _), mux in self.failovers.items():
            if owner_slot != home:
                continue
            status = mux.status()
            status["tuned"] = tuning is not None and tuning.mux is mux
            status["home_name"] = name
            for source in status["sources"]:
                if source["cabinet"] == home:
                    source["name"] = name
            result.append(status)
        return result

    def failover_status(self, token: str):
        """TV links only expose the calling cabinet's sessions and name."""
        with self.lock:
            self._prune_failovers(time.monotonic())
            for row in self.store.all():
                if row["token"] == token:
                    return self._reports_for_cabin(row["slot"], row["name"])
            return None

    def monitor_snapshot(self):
        """Read all cabinets under the same routing lock. Requires the admin credential."""
        with self.lock:
            now = time.monotonic()
            self._prune_failovers(now)
            enabled = not (self.store.directory / "multicdn.disabled").exists()
            viewers = {}
            load = {slot: 0 for slot in (1, 2, 3)}
            for home, tuning in self.tunings.items():
                mux = tuning.mux
                if enabled and mux.history and mux.source_slot is not None:
                    load[mux.source_slot] += 1
                    viewers.setdefault(mux.source_stream, []).append(home)

            result = {}
            health = {}
            profiles = self.store.all()
            for profile in profiles:
                home, token = profile["slot"], profile["token"]
                route = self.routes.get(token)
                if route is None:
                    continue
                manager = route[0]
                with manager.lock:
                    status = json.loads(manager.status(include_standby=True))
                    for stream in status["streams"]:
                        stream["viewers"] = sorted(viewers.get(manager.active[stream["id"]], []))
                reports = self._reports_for_cabin(home, profile["name"])
                tuning = self.tunings.get(home)
                for report in reports:
                    report["tuned"] = enabled and report["tuned"]
                    report["source_viewer_load"] = load
                    if report["tuned"]:
                        usage = self._guest_usage(tuning.mux, now)
                        sources = []
                        for slot, worker in tuning.candidates():
                            if worker not in health:
                                health[worker] = FailoverMux._health(worker)
                            sources.append(dict(health[worker], cabinet=slot, guest_full=(
                                slot != home and usage.get(slot, 0) >= MAX_GUESTS_PER_SOURCE)))
                        report["sources"] = sources
                for cid, old in self.history[home].items():
                    reports.append({
                        "home_cabinet": home, "channel_id": cid, "channel": old.name,
                        "tuned": False, "switches": old.switches, "switch_history": old.events[::-1],
                        "seconds_since_last_probe": round(now - old.last_request, 1), "sources": [],
                    })
                status["multi_cdn"] = reports
                diagnostic = self._diagnostics(token, home, profiles, enabled)
                if enabled:
                    for cabin in diagnostic["cabins"]:
                        if cabin["slot"] == home:
                            cabin["playing"] = tuning is not None
                status["multi_cdn_diagnostics"] = diagnostic
                result[home] = status
            return result

    def pilot_diagnostics(self, token: str):
        """Read-only status for all-channel multi-CDN, without creating streams.

        The method name is kept for compatibility with the gateway status
        route. Public TV tokens reveal only their own viewing activity.
        """
        with self.lock:
            profiles = self.store.all()
            for row in profiles:
                if row["token"] == token:
                    enabled = not (self.store.directory / "multicdn.disabled").exists()
                    return self._diagnostics(token, row["slot"], profiles, enabled)
            return None

    def _diagnostics(self, token, home, profiles, enabled):
        recent = [
            (mux.last_request, cid, mux)
            for (slot, cid), mux in self.failovers.items() if slot == home
        ]
        selected = max(recent, default=None)
        channel_id = selected[1] if selected is not None else None
        owner = self.routes.get(token)
        wanted = owner[0].channels.get(channel_id) if owner and channel_id else None
        cabins = []
        for profile in profiles:
            slot = profile["slot"]
            route = self.routes.get(profile["token"])
            manager = route[0] if route else None
            match = None
            if manager and wanted:
                match = wanted.id if slot == home else self._matching_channel_id(owner[0], manager, wanted)
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
            "enabled": enabled,
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
            for home in list(self.tunings):
                self._end_tuning(home)
            for _, manager in self.managers.values():
                manager.close()
            self.managers.clear()
            self.routes.clear()
