"""Live profile router sharing the existing IPTV HTTP port and Caddy route.

The legacy slot retains its old token and worker while it uses the local file.
Other profiles are loaded from owner-only snapshots and routed by distinct
random path tokens. Saving a profile swaps only that profile's manager.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from iptv_profiles import ProfileStore, atomic_private_write
from iptv_cdn_failover import FailoverMux, SESSION_IDLE_SECONDS, MAX_GUESTS_PER_SOURCE

if TYPE_CHECKING:
    from iptv_multi import ActiveStream, MultiGateway

ARCHIVED_CHANNELS_PER_CABIN = 32


@dataclass
class ChannelHistory:
    name: str
    last_request: float
    switches: int
    events: list


@dataclass
class ManifestCheckpoint:
    channel_id: str
    key: bytes = field(repr=False)
    sequence_limit: int = 0

    @classmethod
    def load(cls, path: Path) -> ManifestCheckpoint | None:
        try:
            # The writer stores three short fields. Bound even a damaged file before parsing.
            with path.open("rb") as stream:
                raw = stream.read(513)
            if len(raw) > 512:
                raise ValueError("oversized checkpoint")
            data = json.loads(raw.decode("ascii"))
            if not isinstance(data, list) or len(data) != 3:
                raise ValueError("invalid checkpoint layout")
            channel, key, limit = data
            if (not isinstance(channel, str) or not re.fullmatch(r"[0-9a-f]{20}", channel)
                    or not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key)
                    or type(limit) is not int or not 0 < limit < 2**64):
                raise ValueError("invalid checkpoint fields")
            return cls(channel, bytes.fromhex(key), limit)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Ignoring unreadable or invalid manifest checkpoint %s", path.name)
            return None

    def reserve(self, path: Path, next_sequence: int) -> None:
        # Write ahead in blocks so a crash cannot reuse a published sequence.
        if next_sequence < self.sequence_limit:
            return
        limit = next_sequence + 1024
        atomic_private_write(path, json.dumps([self.channel_id, self.key.hex(), limit]))
        self.sequence_limit = limit

    def manifest_id(self, device: str) -> str:
        return hmac.digest(self.key, device.encode("ascii"), "sha256")[:12].hex()


@dataclass
class Tuning:
    mux: FailoverMux
    manager: MultiGateway
    checkpoint: ManifestCheckpoint
    players: dict[str | tuple[str, str], float] = field(default_factory=dict, repr=False)
    ended: threading.Event = field(default_factory=threading.Event)
    sources: dict = field(default_factory=dict)  # slot -> (manager, worker)
    unavailable: dict = field(default_factory=dict)
    blocked_reason: tuple | None = None
    reported_switch: dict | None = None

    def note_player(self, player, now):
        if player is not None:
            self.players.pop(player, None)
            self.players[player] = now
            # The two most recent distinct players suffice to detect overlap.
            if len(self.players) > 2:
                del self.players[next(iter(self.players))]

    def diagnostics(self, usage, now):
        mux = self.mux
        sources = {}
        for slot in (1, 2, 3):
            state = mux.source_health.get(slot)
            if state is None:
                sources[slot] = self.unavailable.get(slot, {"state": "WORKER_STOPPED"})
                continue
            sources[slot] = {key: state[key] for key in (
                "ready", "fresh", "age_seconds", "buffer_seconds",
                "published_age_seconds", "published_sequence", "downloaded_sequence")}
            sources[slot]["other_guests"] = usage.get(slot, 0)
        return {"selected_slot": mux.source_slot, "sources": sources,
                "publish_age_seconds": round(now - mux.last_progress, 1) if mux.history else None}

    def record_decision(self, telemetry, channel_key, usage, now):
        mux = self.mux
        selected = mux.source_slot
        if selected is None:
            return  # The manifest timeout records the cold-start snapshot.
        latest = mux.switch_events[-1]
        changed = latest is not self.reported_switch
        if changed:
            self.reported_switch = latest
            reason = latest["reason"]
            self.blocked_reason = None
        elif mux.backup_blocker:
            reason = mux.backup_blocker
            # Buffer ages change every poll. Only log changes in eligibility.
            unavailable = tuple(
                (slot, info["state"], info.get("max_streams"), tuple(
                    (item["channel_id"], item["pinned"], item["standby"], bool(item["pending_requests"]))
                    for item in info.get("active", ())))
                for slot, info in sorted(self.unavailable.items()))
            signature = (reason, selected, unavailable,
                         tuple((slot, s["ready"], s["fresh"], usage.get(slot, 0))
                               for slot, s in sorted(mux.source_health.items())))
            if self.blocked_reason == signature:
                return
            self.blocked_reason = signature
        elif self.blocked_reason is not None:
            reason = "SOURCE_AVAILABLE"
            self.blocked_reason = None
        else:
            return
        context = self.diagnostics(usage, now)
        if changed:
            context["from_slot"] = latest["from_cabinet"]
        telemetry.incident(channel_key, "cdn_decision", reason, diagnostics=context)

    def candidates(self):
        return [(slot, worker) for slot, (_, worker) in self.sources.items()
                if not worker.gateway.stop.is_set()]


@dataclass(frozen=True)
class ManifestSelection:
    tuning: Tuning | None = None
    worker: ActiveStream | None = None
    playlist: bytes | None = None
    superseded: bool = False


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
        self.checkpoints = {slot: ManifestCheckpoint.load(self._checkpoint_path(slot))
                            for slot in (1, 2, 3)}
        self.closed = False
        self.legacy_detached = False
        self._reload()

    def _checkpoint_path(self, home):
        return self.store.directory / f"manifest-session-{home}.json"

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

    def _end_tuning(self, home, keep=()):
        previous = self.tunings.pop(home, None)
        if previous:
            previous.ended.set()
            self._release_unused(set(previous.sources.values()) - set(keep))
            # Keep already issued segment bytes until expiry, without pinning the upstream cache.
            with previous.mux.lock:
                previous.mux.source_stream = None

    def _selected_guest_workers(self, home):
        return {tuning.mux.source_stream for slot, tuning in self.tunings.items()
                if slot != home and tuning.mux.source_slot == home}

    @staticmethod
    def _start_on_reserve(mux, reserves, now):
        for slot, (manager, worker) in reserves.items():
            with manager.lock:
                if (manager.active.get(worker.channel.id) is worker
                        and mux.start_from_ready(slot, worker, now)):
                    worker.accessed = now
                    return True
        return False

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

    def begin_manifest(self, token: str, cid: str, player: str | tuple[str, str], *, session=None, denial=None):
        """Select and first publish atomically with respect to other tunings."""
        with self.lock:
            if session is not None:
                home = self._slot_for_token(token)
                saved = self.checkpoints.get(home)
                if (saved is None or saved.channel_id != cid or not isinstance(player, str)
                        or not hmac.compare_digest(saved.manifest_id(player), session)):
                    return ManifestSelection(superseded=True)
            selection = self.failover_for(token, cid, player=player, denial=denial)
            if selection is None:
                return None  # Native HLS mode.
            mux, candidates = selection
            if mux is None:
                return ManifestSelection()  # All usable slots are busy.
            tuning = self.tunings[mux.home_slot]
            worker = dict(candidates).get(mux.home_slot)
            playlist = self.publish_failover(mux, token, request=tuning, player=player)
            if worker is not None:
                with tuning.manager.lock:
                    worker.pending_requests += 1
            return ManifestSelection(tuning, worker, playlist)

    def _failover_enabled(self):
        return not self.closed and not (self.store.directory / "multicdn.disabled").exists()

    def failover_for(self, token: str, cid: str, *, player=None, denial=None):
        """Use every channel's HOME Edem subscription with optional backups.

        Standby streams are started on demand for the *same* channel only;
        they never evict an active viewer of a different channel. A channel
        missing in a backup stays playable from its owner's own source.
        """
        with self.lock:
            if not self._failover_enabled():
                for slot in list(self.tunings):
                    self._end_tuning(slot)
                return None
            home = self._slot_for_token(token)
            owner = self.routes.get(token)
            if home is None or owner is None or cid not in owner[0].channels:
                return None
            manager = owner[0]
            wanted = manager.channels[cid]
            now = time.monotonic()
            self._prune_failovers(now)
            tuning = self.tunings.get(home)
            retuning = tuning is not None and tuning.mux.channel_id != cid
            dedicated = player is not None and manager.prefer_owner_tuning and manager.max_streams == 1
            superseded = None
            protected = ()
            if dedicated:
                if retuning and any(other != player and now - seen < SESSION_IDLE_SECONDS
                                    for other, seen in tuning.players.items()):
                    manager.record_capacity_denial(denial)
                    return None, []
                # IP and User-Agent can distinguish some legacy viewers, but
                # matching signatures do not prove the same device.
                if retuning and isinstance(player, str) and player in tuning.players and home in tuning.sources:
                    superseded = tuning.sources[home][1]
                protected = self._selected_guest_workers(home)
            worker = manager.start(cid, denial=denial, superseded=superseded,
                                   protected=protected, count_denial=False)
            if worker is None and not dedicated:
                manager.record_capacity_denial(None)
                return None, []
            # A new selection can bypass a busy home source with an already
            # playable reserve. Keep the existing tuning if there is none.
            require_ready = worker is None and (tuning is None or retuning or not tuning.mux.history)
            key = (home, cid)
            mux = self.failovers.get(key)
            new_mux = mux is None
            checkpoint = self.checkpoints[home]
            resuming = checkpoint is not None and checkpoint.channel_id == cid
            if new_mux:
                first_sequence = self.next_sequences.get(key, 1)
                if resuming:
                    first_sequence = max(first_sequence, checkpoint.sequence_limit)
                mux = FailoverMux(home_slot=home, channel_id=cid, channel_name=wanted.name,
                                  first_sequence=first_sequence)
                if resuming:
                    mux.discontinuity_sequence = checkpoint.sequence_limit
                history = self.history[home].get(cid)
                if history:
                    mux.switches = history.switches
                    mux.switch_events = history.events.copy()
            if require_ready:
                reserves, unavailable = self._reserves(home, manager, wanted, ready_only=True)
                if not self._start_on_reserve(mux, reserves, now):
                    manager.record_capacity_denial(None)
                    return None, []
            if tuning is None or retuning:
                if not resuming:
                    checkpoint = ManifestCheckpoint(cid, secrets.token_bytes(32))
                checkpoint.reserve(self._checkpoint_path(home), mux.next_sequence)
                self.checkpoints[home] = checkpoint
                self._end_tuning(home, reserves.values() if require_ready else ())
                if new_mux:
                    self.next_sequences.pop(key, None)
                    self.history[home].pop(cid, None)
                    self.failovers[key] = mux
                elif not require_ready:
                    mux.restart_session(time.monotonic())
                tuning = self.tunings[home] = Tuning(mux, manager, checkpoint)
            tuning.note_player(player, now)
            if not require_ready:
                reserves, unavailable = self._reserves(home, manager, wanted)
            sources = {home: (manager, worker)} if worker is not None else {}
            if worker is None:
                unavailable[home] = {"state": "STREAM_LIMIT", **(denial or {})}
            sources.update(reserves)
            dropped = set(tuning.sources.values()) - set(sources.values())
            tuning.sources = sources
            tuning.unavailable = unavailable
            self._release_unused(dropped)
            return tuning.mux, tuning.candidates()

    def _reserves(self, home, owner, wanted, *, ready_only=False):
        sources = {}
        unavailable = {}
        usage = self._guest_usage(home, time.monotonic()) if ready_only else {}
        for profile in self.store.all():
            slot = profile["slot"]
            if slot == home:
                continue
            route = self.routes.get(profile["token"])
            if not route:
                unavailable[slot] = {"state": "NOT_CONFIGURED"}
                continue
            manager = route[0]
            matching_id = self._matching_channel_id(owner, manager, wanted)
            if matching_id is None:
                unavailable[slot] = {"state": "NO_UNIQUE_CHANNEL_MATCH"}
                continue
            if ready_only:
                with manager.lock:
                    worker = manager.active.get(matching_id)
                    if worker is not None and usage.get(slot, 0) < MAX_GUESTS_PER_SOURCE:
                        sources[slot] = (manager, worker)
            else:
                capacity = {} if owner.telemetry is not None else None
                worker = manager.start(matching_id, standby=True, denial=capacity, count_denial=False)
                if worker is not None:
                    sources[slot] = (manager, worker)
                else:
                    unavailable[slot] = {"state": "STREAM_LIMIT", **(capacity or {})}
        return sources, unavailable

    def publish_failover(self, mux: FailoverMux, token: str, *, request=None, player=None) -> bytes | None:
        """Serialize CDN placement decisions across users for stable capacity.

        The usage snapshot never counts inactive TV sessions. A healthy
        home CDN always takes priority; each source may carry only one
        guest TV, preserving space for its owner's own playback.
        """
        with self.lock:
            now = time.monotonic()
            current = self.tunings.get(mux.home_slot)
            if current is None or current.mux is not mux or (request is not None and current is not request):
                return None  # An earlier manifest request finished warming after a retune.
            current.note_player(player, now)
            usage = self._guest_usage(mux.home_slot, now)
            playlist = mux.playlist(token, current.candidates(), now=now, usage=usage)
            if playlist:
                current.checkpoint.reserve(self._checkpoint_path(mux.home_slot), mux.next_sequence)
            if playlist and mux.source_slot != mux.home_slot:
                selected = current.sources.get(mux.source_slot)
                if selected is not None:
                    source_manager, worker = selected
                    with source_manager.lock:
                        if (worker is mux.source_stream
                                and source_manager.active.get(worker.channel.id) is worker):
                            worker.accessed = now
            manager = current.manager
            if manager.telemetry is not None:
                current.record_decision(manager.telemetry, manager.telemetry_channel_id(mux.channel_id),
                                        usage, now)
            return playlist

    def finish_manifest(self, tuning, *, timed_out):
        """Decide cancellation and timeout diagnostics against the same tuning."""
        with self.lock:
            home = tuning.mux.home_slot
            if self.tunings.get(home) is not tuning:
                return True, {}
            if timed_out and tuning.manager.telemetry is not None:
                now = time.monotonic()
                return False, tuning.diagnostics(self._guest_usage(home, now), now)
            return False, {}

    def _guest_usage(self, home, now):
        usage = {}
        for tuning in self.tunings.values():
            other = tuning.mux
            slot = other.source_slot
            if (other.home_slot != home and other.history and slot not in (None, other.home_slot)
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
                        usage = self._guest_usage(tuning.mux.home_slot, now)
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
