"""Per-channel HLS CDN failover: one virtual timeline per TV and channel.

Every on-demand channel uses its TV cabin's own upstream as the primary source.
Each source is pulled by its own existing Gateway worker/cache; never splice
upstream HLS sequence numbers, timestamps or provider URLs together.
"""
from __future__ import annotations

import math
import secrets
import threading
import time
from dataclasses import dataclass
from weakref import ReferenceType, WeakKeyDictionary, ref

PILOT_NAME = "vf теория большого взрыва"
WINDOW_SEGMENTS = 12
MAX_VIRTUAL_BYTES = 64 * 1024 * 1024
SWITCH_COOLDOWN = 18.0
PUBLISH_STALL_SECONDS = 18.0
SESSION_IDLE_SECONDS = 45.0
MAX_GUESTS_PER_SOURCE = 1  # Reserve capacity for the source's owner.


def is_pilot(name: str) -> bool:
    return " ".join(name.casefold().split()) == PILOT_NAME


@dataclass(frozen=True)
class VirtualSegment:
    number: int
    duration: float
    content: bytes
    discontinuity: bool


@dataclass
class HomeProgress:
    worker: ReferenceType
    epoch: int
    sequence: int
    since: float
    last_advance: float


class FailoverMux:
    """Emit monotonic virtual HLS sequences across complete source switches.

    An immutable copy/reference of each published transport-stream segment
    remains available for old TV segment requests even if its source worker
    advances or rotates. EXT-X-DISCONTINUITY resets the decoder on a change.
    No attempt is made to align programme timestamps across separate CDNs.
    """

    def __init__(self, home_slot: int = 1, *, channel_id: str | None = None,
                 channel_name: str = "VF Теория большого взрыва", first_sequence: int = 1):
        if home_slot not in (1, 2, 3):
            raise ValueError("Unknown home cabin")
        self.home_slot = home_slot
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.lock = threading.RLock()
        self.source_slot = None
        self.source_stream = None
        self.home_progress = None
        self.generation = secrets.token_hex(12)
        self.next_sequence = first_sequence
        self.history: list[VirtualSegment] = []
        self.discontinuity_sequence = 0
        self.target_duration = 1
        self.switches = 0
        self.switch_events: list[dict] = []  # Last 20 choices, sanitized.
        self.last_switch = 0.0
        self.backup_blocker = None
        self.last_progress = 0.0
        self.last_request = time.monotonic()
        # Keep cursors across switches and idle sessions, without retaining old caches.
        self.published_cursors = WeakKeyDictionary()  # worker -> (cache epoch, sequence)
        self.slots_seen: list[int] = []
        self.source_health: dict[int, dict] = {}

    @staticmethod
    def _health(stream) -> dict:
        details = stream.gateway.status()
        import json
        data = json.loads(details)
        age = data.get("seconds_since_segment_download")
        ready = bool(data.get("ready"))
        # A stale cache can remain READY long after the provider has stopped.
        fresh = ready and not data.get("upstream_stalled") and age is not None and age < 20
        return {
            "ready": ready,
            "fresh": fresh,
            "age_seconds": age,
            "buffer_seconds": data.get("effective_buffer_delay_seconds"),
            "contiguous_tail_seconds": data.get("contiguous_tail_seconds"),
            "published_age_seconds": data.get("seconds_since_published_advance"),
            "published_sequence": data.get("last_published_sequence"),
            "downloaded_sequence": data.get("last_downloaded_sequence"),
            "cached_mib": data.get("cached_mib"),
            "errors": data.get("errors"),
            "slow_segments": data.get("slow_segments"),
        }

    def _alternates(self, streams, usage: dict[int, int]):
        """Prefer the viewer's own source, otherwise choose an underused backup.

        A reserve already serving two TV cabins cannot attract a third,
        even if it has more buffer or faster recent downloads.
        """
        eligible = [slot for slot, stream in streams.items()
                    if stream is not self.source_stream
                    and self.source_health[slot]["fresh"]
                    and (slot == self.home_slot or
                         usage.get(slot, 0) < MAX_GUESTS_PER_SOURCE)]
        return sorted(eligible, key=lambda slot: (
            slot == self.home_slot,
            -usage.get(slot, 0),
            self.source_health[slot]["buffer_seconds"] or 0,
            -(self.source_health[slot]["age_seconds"] or 0),
            -slot
        ), reverse=True)

    def _home_recovered(self, home, now: float) -> bool:
        """Observe the playable edge without moving the home gateway's cursor."""
        if (home is None or self.source_slot in (None, self.home_slot)
                or not self.source_health[self.home_slot]["fresh"]):
            self.home_progress = None
            return False
        with home.gateway.lock:
            epoch = home.gateway.cache_epoch
            window = home.gateway.published(commit=False)
        if not window:
            self.home_progress = None
            return False

        sequence = window[-1][0]
        progress = self.home_progress
        if (progress is None or home is not progress.worker() or epoch != progress.epoch
                or sequence < progress.sequence
                or now - progress.last_advance >= PUBLISH_STALL_SECONDS):
            self.home_progress = HomeProgress(ref(home), epoch, sequence, now, now)
            return False
        if sequence == progress.sequence:
            return False
        progress.sequence = sequence
        progress.last_advance = now
        return now - progress.since >= SWITCH_COOLDOWN

    def _append(self, duration: float, payload: bytes, *, discontinuity: bool):
        self.history.append(VirtualSegment(self.next_sequence, duration, payload, discontinuity))
        self.target_duration = max(self.target_duration, math.ceil(duration))
        self.next_sequence += 1
        while (len(self.history) > WINDOW_SEGMENTS
               or (len(self.history) > 3 and
                   sum(len(item.content) for item in self.history) > MAX_VIRTUAL_BYTES)):
            removed = self.history.pop(0)
            if removed.discontinuity:
                self.discontinuity_sequence += 1

    def restart_session(self, now: float):
        """Start a new viewing session without reusing stale video or sequence IDs."""
        with self.lock:
            self.discontinuity_sequence += sum(segment.discontinuity for segment in self.history)
            self.history.clear()
            self.source_slot = None
            self.source_stream = None
            self.home_progress = None
            self.last_progress = 0.0
            self.last_switch = 0.0
            self.last_request = now
            # Keep next_sequence and each worker's last published cursor.

    def _publish_from(self, slot, stream, now: float, *, reason: str = "START") -> bool:
        """Select a worker only when it contributes playable, unseen video."""
        gateway = stream.gateway
        switching = stream is not self.source_stream
        with gateway.lock:
            epoch = gateway.cache_epoch
            previous_epoch, last = self.published_cursors.get(stream, (epoch, None))
            reset = epoch != previous_epoch
            if reset:
                last = None
            if switching and last is not None:
                # Looking at a previously used standby must not move its player's cursor.
                window = gateway.published(commit=False)
                if not window or window[-1][0] <= last:
                    return False
            window = gateway.published()
            if last is not None:
                window = [row for row in window if row[0] > last]
            if switching or reset:
                window = window[-3:]
            records = [(seq, duration, gateway.segment(seq)) for seq, duration in window]

        if not records:
            return False
        boundary = switching or reset
        for seq, duration, payload in records:
            gap = last is not None and seq != last + 1
            self._append(duration, payload,
                         discontinuity=self.next_sequence > 1 and (boundary or gap))
            last = seq
            boundary = False

        if switching:
            prior = self.source_slot
            if prior is not None:
                self.switches += 1
            self.last_switch = now
            self.source_slot = slot
            self.source_stream = stream
            # Never expose provider URLs, account tokens or worker IDs.
            self.switch_events.append({
                "at": round(time.time()),
                "from_cabinet": prior,
                "to_cabinet": slot,
                "reason": reason if prior is not None else "START",
                "kind": "switch" if prior is not None else "start",
            })
            if len(self.switch_events) > 20:
                del self.switch_events[:-20]
        self.published_cursors[stream] = (epoch, last)
        self.last_progress = now
        return True

    def playlist(self, token: str, candidates: list[tuple[int, object]], *,
                 now: float | None = None, usage: dict[int, int] | None = None):
        """Emit one stable HLS timeline, failing over on *published* video stalls.

        A CDN can continue downloading while the TV-facing HLS playlist is
        frozen on a gap. Check progress after trying the current source, not
        just the age of its last downloaded segment.
        """
        now = time.monotonic() if now is None else now
        sources = {slot: stream for slot, stream in candidates}
        usage = usage or {}
        with self.lock:
            if (self.history and now >= self.last_request
                    and now - self.last_request >= SESSION_IDLE_SECONDS):
                # A stopped TV must not be served segments from the old session.
                self.restart_session(now)
            self.last_request = now
            self.slots_seen = sorted(sources)
            self.source_health = {slot: self._health(stream) for slot, stream in sources.items()}
            selected = self.source_slot
            current = sources.get(selected)
            if current is not self.source_stream:
                current = None

            fresh = current is not None and self.source_health[selected]["fresh"]
            if fresh:
                self._publish_from(selected, current, now)

            stalled = now - self.last_progress >= PUBLISH_STALL_SECONDS
            can_switch = current is None or now - self.last_switch >= SWITCH_COOLDOWN
            home = sources.get(self.home_slot)
            home_recovered = self._home_recovered(home, now)
            self.backup_blocker = None
            if can_switch and (not fresh or stalled):
                reason = ("SOURCE_RESTARTED" if current is None and selected is not None else
                          "SOURCE_UNAVAILABLE" if not fresh else
                          "PUBLISH_STALL")
                for slot in self._alternates(sources, usage):
                    if self._publish_from(slot, sources[slot], now, reason=reason):
                        break
                else:
                    self.backup_blocker = "NO_READY_BACKUP"
            elif not can_switch and (not fresh or stalled):
                self.backup_blocker = "SWITCH_COOLDOWN"
            elif can_switch and home_recovered:
                self._publish_from(self.home_slot, home, now, reason="HOME_RECOVERED")

            # A stale provider can still have minutes of playable local video.
            if not fresh and current is not None and current is self.source_stream:
                self._publish_from(selected, current, now)

            if not self.history:
                return None
            target = self.target_duration  # Remain stable across HLS window changes.
            output = [
                "#EXTM3U", "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{target}",
                f"#EXT-X-MEDIA-SEQUENCE:{self.history[0].number}",
                f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.discontinuity_sequence}",
            ]
            for entry in self.history:
                if entry.discontinuity:
                    output.append("#EXT-X-DISCONTINUITY")
                output.append(f"#EXTINF:{entry.duration:.3f},")
                output.append(f"/{token}/segment/{self.generation}.{entry.number}.ts")
            return ("\n".join(output) + "\n").encode("utf-8")

    def segment(self, sequence: int) -> bytes | None:
        with self.lock:
            for entry in self.history:
                if entry.number == sequence:
                    return entry.content
        return None

    def status(self) -> dict:
        with self.lock:
            return {
                "enabled": True,
                "channel": self.channel_name,
                "channel_id": self.channel_id,
                "home_cabinet": self.home_slot,
                "selected_cabinet": self.source_slot,
                "available_cabinets": list(self.slots_seen),
                "switches": self.switches,
                "switch_history": list(reversed(self.switch_events)),
                "last_playlist_progress_seconds": (
                    round(max(0.0, time.monotonic() - self.last_progress), 1)
                    if self.last_progress else None),
                "seconds_since_last_probe": round(max(0.0, time.monotonic() - self.last_request), 1),
                "virtual_segment_sequence": self.history[-1].number if self.history else None,
                "cached_video_seconds": round(sum(segment.duration for segment in self.history), 1),
                "sources": [
                    {"cabinet": slot, **data}
                    for slot, data in sorted(self.source_health.items())
                ],
            }
