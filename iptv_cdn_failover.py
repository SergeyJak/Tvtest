"""Pilot HLS CDN failover: one local virtual timeline, three existing TV profiles.

Only the explicitly matched channel in the primary TV cabinet participates.
Each source is pulled by its own existing Gateway worker/cache; never splice
upstream HLS sequence numbers, timestamps or provider URLs together.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

PILOT_NAME = "vf теория большого взрыва"
WINDOW_SEGMENTS = 12
MAX_VIRTUAL_BYTES = 64 * 1024 * 1024
SWITCH_COOLDOWN = 18.0
PUBLISH_STALL_SECONDS = 18.0
SESSION_IDLE_SECONDS = 45.0


def is_pilot(name: str) -> bool:
    return " ".join(name.casefold().split()) == PILOT_NAME


@dataclass(frozen=True)
class VirtualSegment:
    number: int
    duration: float
    content: bytes
    discontinuity: bool


class FailoverMux:
    """Emit monotonic virtual HLS sequences across complete source switches.

    An immutable copy/reference of each published transport-stream segment
    remains available for old TV segment requests even if its source worker
    advances or rotates. EXT-X-DISCONTINUITY resets the decoder on a change.
    No attempt is made to align programme timestamps across separate CDNs.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.source_slot = None
        self.source_stream = None
        self.last_upstream_seq = None
        self.next_sequence = 1
        self.history: list[VirtualSegment] = []
        self.discontinuity_sequence = 0
        self.target_duration = 1
        self.switches = 0
        self.last_switch = 0.0
        self.last_progress = 0.0
        self.last_request = time.monotonic()
        self.retired_source = None  # (old worker, last TV-published source sequence)
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
            "cached_mib": data.get("cached_mib"),
            "errors": data.get("errors"),
            "slow_segments": data.get("slow_segments"),
        }

    def _candidate(self, streams: dict[int, object], *, exclude: int | None = None):
        eligible = [slot for slot, info in self.source_health.items()
                    if info["fresh"] and slot in streams and slot != exclude]
        if not eligible:
            return None
        if self.source_slot in eligible and self.source_stream is streams[self.source_slot]:
            return self.source_slot
        if self.source_slot is None and 1 in eligible:
            return 1  # Prefer the viewer's own account for a fresh session.
        return max(eligible, key=lambda slot: (
            self.source_health[slot]["buffer_seconds"] or 0,
            -(self.source_health[slot]["age_seconds"] or 0),
            -slot
        ))

    def _append(self, sequence: int, duration: float, payload: bytes, *, discontinuity: bool):
        self.history.append(VirtualSegment(self.next_sequence, duration, payload, discontinuity))
        self.target_duration = max(self.target_duration, math.ceil(duration))
        self.next_sequence += 1
        while (len(self.history) > WINDOW_SEGMENTS
               or (len(self.history) > 3 and
                   sum(len(item.content) for item in self.history) > MAX_VIRTUAL_BYTES)):
            removed = self.history.pop(0)
            if removed.discontinuity:
                self.discontinuity_sequence += 1

    def _retire_history(self):
        """Start a new viewing session without reusing stale video or sequence IDs."""
        self.discontinuity_sequence += sum(segment.discontinuity for segment in self.history)
        # A still-running gateway can advertise its old published playlist
        # after a viewer returns. Require a *new* source sequence from that
        # same worker before reusing its video.
        self.retired_source = (self.source_stream, self.last_upstream_seq)
        self.history.clear()
        self.source_slot = None
        self.source_stream = None
        self.last_upstream_seq = None
        self.last_progress = 0.0
        self.last_switch = 0.0
        # next_sequence intentionally remains monotonic.

    def _publish_from(self, stream, now: float) -> bool:
        """Copy newly offered video from the selected Gateway into our timeline."""
        records = stream.gateway.published()
        records = [(seq, duration, stream.gateway.segment(seq))
                   for seq, duration in records]
        records = [(seq, duration, data) for seq, duration, data in records if data]
        if not records:
            return False
        if self.last_upstream_seq is None and self.retired_source is not None:
            old_stream, old_seq = self.retired_source
            if old_stream is stream and old_seq is not None:
                records = [row for row in records if row[0] > old_seq]
                if not records:
                    return False  # Never replay the old session's last window.
        if self.last_upstream_seq is None:
            selected_records = records[-min(3, len(records)):]
            discontinuity = bool(self.history)
        elif records[-1][0] < self.last_upstream_seq - 3:
            # Same upstream restarted its media-sequence numbering.
            selected_records = records[-min(3, len(records)):]
            discontinuity = True
        else:
            selected_records = [row for row in records if row[0] > self.last_upstream_seq]
            discontinuity = bool(
                selected_records and selected_records[0][0] > self.last_upstream_seq + 1)
        for index, (seq, duration, data) in enumerate(selected_records):
            self._append(seq, duration, data,
                         discontinuity=discontinuity and index == 0)
            self.last_upstream_seq = seq
            self.last_progress = now
        if selected_records:
            self.retired_source = None
        return bool(selected_records)

    def playlist(self, token: str, candidates: list[tuple[int, object]], *, now: float | None = None):
        """Emit one stable HLS timeline, failing over on *published* video stalls.

        A CDN can continue downloading while the TV-facing HLS playlist is
        frozen on a gap. Check progress after trying the current source, not
        just the age of its last downloaded segment.
        """
        now = time.monotonic() if now is None else now
        sources = {slot: stream for slot, stream in candidates}
        with self.lock:
            if (self.history and now >= self.last_request
                    and now - self.last_request >= SESSION_IDLE_SECONDS):
                # A stopped TV must not be served segments from the old session.
                self._retire_history()
            self.last_request = now
            self.slots_seen = sorted(sources)
            self.source_health = {slot: self._health(stream) for slot, stream in sources.items()}
            selected = self.source_slot
            current = sources.get(selected)
            if current is not self.source_stream:
                current = None

            if current is not None and self.source_health.get(selected, {}).get("fresh"):
                self._publish_from(current, now)

            stalled = bool(self.history and self.last_progress
                           and now - self.last_progress >= PUBLISH_STALL_SECONDS)
            healthy = (current is not None
                       and self.source_health.get(selected, {}).get("fresh")
                       and not stalled)
            can_switch = (selected is None or now - self.last_switch >= SWITCH_COOLDOWN)
            if not healthy and can_switch:
                # Never reselect a source merely because it downloads segments
                # if its published TV playlist stopped advancing.
                alternate = self._candidate(sources, exclude=selected if stalled else None)
                if alternate is not None and (alternate != selected or current is None):
                    self.source_slot = alternate
                    self.source_stream = sources[alternate]
                    self.last_upstream_seq = None
                    self.last_switch = now
                    self.switches += bool(self.history)
                    self._publish_from(self.source_stream, now)

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
                output.append(f"/{token}/segment/{entry.number}.ts")
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
                "channel": "VF Теория большого взрыва",
                "selected_cabinet": self.source_slot,
                "available_cabinets": list(self.slots_seen),
                "switches": self.switches,
                "sources": [
                    {"cabinet": slot, **data}
                    for slot, data in sorted(self.source_health.items())
                ],
            }
