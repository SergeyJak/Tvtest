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
SWITCH_COOLDOWN = 18.0


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
        self.switches = 0
        self.last_switch = 0.0
        self.last_progress = 0.0
        self.last_request = time.monotonic()
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

    def _candidate(self, streams: dict[int, object], now: float):
        eligible = [slot for slot, info in self.source_health.items()
                    if info["fresh"] and slot in streams]
        if not eligible:
            return None
        if self.source_slot in eligible and self.source_stream is streams[self.source_slot]:
            return self.source_slot
        if self.source_slot is None and 1 in eligible:
            return 1  # Keep the original cabinet the preferred source.
        return max(eligible, key=lambda slot: (
            self.source_health[slot]["buffer_seconds"] or 0,
            -(self.source_health[slot]["age_seconds"] or 0),
            -slot
        ))

    def _append(self, sequence: int, duration: float, payload: bytes, *, discontinuity: bool):
        self.history.append(VirtualSegment(self.next_sequence, duration, payload, discontinuity))
        self.next_sequence += 1
        while len(self.history) > WINDOW_SEGMENTS:
            removed = self.history.pop(0)
            if removed.discontinuity:
                self.discontinuity_sequence += 1

    def playlist(self, token: str, candidates: list[tuple[int, object]], *, now: float | None = None):
        """Return the virtual playlist or None while every source warms/stalls."""
        import json
        now = time.monotonic() if now is None else now
        sources = {slot: stream for slot, stream in candidates}
        with self.lock:
            self.last_request = now
            self.slots_seen = sorted(sources)
            self.source_health = {slot: self._health(stream) for slot, stream in sources.items()}
            selected = self.source_slot
            current = sources.get(selected)
            if current is not self.source_stream:
                current = None

            selected_healthy = (current is not None
                                and self.source_health.get(selected, {}).get("fresh"))
            can_switch = now - self.last_switch >= SWITCH_COOLDOWN
            if not selected_healthy and can_switch:
                alternate = self._candidate(sources, now)
                if alternate is not None and (alternate != selected or current is None):
                    selected = alternate
                    current = sources[alternate]
                    self.source_slot = selected
                    self.source_stream = current
                    self.last_upstream_seq = None
                    self.last_switch = now
                    self.switches += bool(self.history)
            if current is not None:
                # Using commit=True only on the currently selected worker,
                # never on standbys or other cabinets' playback cursors.
                records = current.gateway.published()
                records = [(seq, duration, current.gateway.segment(seq))
                           for seq, duration in records]
                records = [(seq, duration, data) for seq, duration, data in records if data]
                if records:
                    if self.last_upstream_seq is None:
                        # New decoder epoch: start near the live edge, and mark
                        # a discontinuity after the previous CDN's video.
                        selected_records = records[-min(3, len(records)):]
                        discontinuity = bool(self.history)
                    else:
                        if records[-1][0] < self.last_upstream_seq - 3:
                            # Provider reset the HLS media sequence on the
                            # same CDN. Begin a fresh decoder epoch rather
                            # than freezing until its counter catches up.
                            selected_records = records[-min(3, len(records)):]
                            discontinuity = True
                        else:
                            selected_records = [row for row in records
                                                if row[0] > self.last_upstream_seq]
                            discontinuity = bool(
                                selected_records
                                and selected_records[0][0] > self.last_upstream_seq + 1)
                    for index, (seq, duration, data) in enumerate(selected_records):
                        self._append(seq, duration, data,
                                     discontinuity=discontinuity and index == 0)
                        self.last_upstream_seq = seq
                        self.last_progress = now

            if not self.history:
                return None
            target = max(1, math.ceil(max(row.duration for row in self.history)))
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
