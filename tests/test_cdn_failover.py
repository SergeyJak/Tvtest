"""Three-CDN VF pilot: no private URLs, no cross-account paths, no HLS mixing."""
import json
import threading
import unittest
from unittest.mock import patch

from iptv_cdn_failover import FailoverMux, is_pilot
from iptv_gateway import Gateway


class FakeGateway:
    def __init__(self, label):
        self.lock = threading.RLock()
        self.cache_epoch = 0
        self.label = label
        self.fresh = True
        self.segments = [(100, 6.0), (101, 6.0), (102, 6.0)]
        self.payload = {seq: f"{label}-{seq}".encode() for seq, _ in self.segments}

    def published(self, commit=True):
        return list(self.segments)

    def segment(self, seq, record_miss=False):
        return self.payload.get(seq)

    def status(self):
        return json.dumps({
            "ready": True,
            "upstream_stalled": not self.fresh,
            "seconds_since_segment_download": 1.0 if self.fresh else 40.0,
            "effective_buffer_delay_seconds": 24.0,
            "cached_mib": 0.01, "errors": 0, "slow_segments": 0,
        }).encode()


class FakeStream:
    def __init__(self, label):
        self.gateway = FakeGateway(label)


class MultiCdnTests(unittest.TestCase):
    def test_soak_three_tvs_and_overlapping_cdn_outages(self):
        """Simulate 12 minutes of HLS polling with two overlapping CDN failures.

        Real provider traffic and TV decoders are out of scope. Validate that
        each viewer keeps a monotonic HLS timeline, no CDN gets two guests,
        the home source remains preferred, and healthy homes are reclaimed.
        """
        sources = {slot: FakeStream(f"CDN-{slot}") for slot in (1, 2, 3)}
        viewers = {home: FailoverMux(home_slot=home, channel_name=f"TV-{home}")
                   for home in (1, 2, 3)}
        last_sequence = {home: 0 for home in viewers}
        out_count = {home: 0 for home in viewers}
        for step in range(121):
            now = 100 + step * 6
            down = ({1} if 20 <= step < 35 else set()) | (
                {2} if 24 <= step < 39 else set())
            for slot, stream in sources.items():
                gateway = stream.gateway
                gateway.fresh = slot not in down
                if gateway.fresh and step:
                    seq = 102 + step
                    gateway.segments.append((seq, 6.0))
                    gateway.payload[seq] = f"{slot}-{seq}".encode()
                    gateway.segments = gateway.segments[-25:]

            # Mirrors the registry's serial placement decision: only guests
            # count toward MAX_GUESTS_PER_SOURCE, owners reserve their place.
            for home, mux in viewers.items():
                usage = {}
                for other_home, other in viewers.items():
                    if other_home == home:
                        continue
                    if other.source_slot not in (None, other_home):
                        usage[other.source_slot] = usage.get(other.source_slot, 0) + 1
                manifest = mux.playlist(
                    f"viewer-{home}", list(sources.items()), now=now, usage=usage)
                self.assertIsNotNone(manifest, f"viewer {home}, tick {step}")
                self.assertLessEqual(len(mux.history), 12)
                self.assertNotIn(b"https://", manifest)
                if mux.history[-1].number > last_sequence[home]:
                    last_sequence[home] = mux.history[-1].number
                    out_count[home] += 1

            selected = [mux.source_slot for mux in viewers.values()]
            self.assertLessEqual(max(selected.count(slot) for slot in sources), 2,
                                 f"Three TVs collapsed onto one CDN at tick {step}")
            for slot in sources:
                self.assertLessEqual(sum(
                    viewer.source_slot == slot and home != slot
                    for home, viewer in viewers.items()), 1)
            if step < 20 or step >= 45:
                # No intermittent failure: after 18s verified recovery,
                # all viewers should be back on their own subscription.
                if step >= 45:
                    self.assertEqual(selected, [1, 2, 3])
            for home, mux in viewers.items():
                if step < 20:
                    self.assertEqual(mux.source_slot, home)
        self.assertEqual([mux.source_slot for mux in viewers.values()], [1, 2, 3])
        self.assertTrue(all(mux.switches >= 2 for home, mux in viewers.items()
                            if home in (1, 2)))
        self.assertTrue(all(count >= 90 for count in out_count.values()))

    def test_pilot_is_exact_and_case_insensitive(self):
        self.assertTrue(is_pilot("  VF  Теория большого взрыва  "))
        self.assertFalse(is_pilot("Теория большого взрыва"))
        self.assertFalse(is_pilot("VF Теория большого взрыва 2"))
        self.assertFalse(is_pilot("VF Кинокомедия HD"))

    def test_each_cabin_prefers_its_own_cdn_even_if_backup_has_more_buffer(self):
        for home_slot in (1, 2, 3):
            mux = FailoverMux(home_slot=home_slot)
            sources = [(slot, FakeStream("CDN-" + str(slot))) for slot in (1, 2, 3)]
            sources[(home_slot % 3)][1].gateway.status = lambda: json.dumps({
                "ready": True, "upstream_stalled": False,
                "seconds_since_segment_download": 0.1,
                "effective_buffer_delay_seconds": 90.0,
                "contiguous_tail_seconds": 180.0,
                "errors": 0, "slow_segments": 0,
            }).encode()
            mux.playlist("owner-token", sources, now=100)
            self.assertEqual(mux.source_slot, home_slot)
            self.assertEqual(mux.home_slot, home_slot)
            self.assertEqual(mux.switches, 0)

    def test_one_guest_per_source_blocks_collapsing_three_viewers(self):
        mux = FailoverMux(home_slot=1)
        home = FakeStream("owner")
        two = FakeStream("two")
        three = FakeStream("three")
        home.gateway.fresh = False
        mux.playlist("tv", [(1, home), (2, two), (3, three)],
                     now=100, usage={2: 1, 3: 0})
        self.assertEqual(mux.source_slot, 3, "The occupied CDN cannot take another guest")
        self.assertEqual(mux.segment(1), b"three-100")
        # When every backup is already carrying another guest, it is safer
        # to keep buffering than to move three users onto a single account.
        mux2 = FailoverMux(home_slot=1)
        self.assertIsNone(mux2.playlist("tv", [(1, home), (2, two), (3, three)],
                                         now=100, usage={2: 1, 3: 1}))
        self.assertIsNone(mux2.source_slot)

    def test_warming_home_reclaims_viewer_after_sustained_playable_progress(self):
        mux = FailoverMux(home_slot=2)
        backup, home = FakeStream("backup"), FakeStream("home")
        sources = [(1, backup), (2, home)]
        home.gateway.fresh = False
        mux.playlist("tv", sources, now=100)
        self.assertEqual(mux.source_slot, 1)
        home.gateway.fresh = True
        for step in range(1, 10):
            seq = 102 + step
            backup.gateway.segments.append((seq, 6.0))
            backup.gateway.payload[seq] = b"backup"
            # Fresh download status alone cannot establish home recovery.
            if step > 5:
                home.gateway.segments.append((seq, 6.0))
                home.gateway.payload[seq] = b"home"
            mux.playlist("tv", sources, now=100 + 6 * step)
            if step <= 5:
                self.assertEqual(mux.source_slot, 1)
        self.assertEqual(mux.source_slot, 2)
        self.assertEqual(mux.switches, 1)
        self.assertEqual(mux.switch_events[-1]["reason"], "HOME_RECOVERED")
        self.assertEqual(mux.history[-1].content, b"home")

    def test_good_owner_is_sticky_not_switched_for_faster_backup(self):
        mux = FailoverMux(home_slot=2)
        sources = [(slot, FakeStream("CDN-" + str(slot))) for slot in (1, 2, 3)]
        mux.playlist("tv", sources, now=100)
        self.assertEqual(mux.source_slot, 2)
        sources[1][1].gateway.fresh = True
        sources[0][1].gateway.segments.append((103, 6.0))
        sources[0][1].gateway.payload[103] = b"fast-new-103"
        sources[1][1].gateway.segments.append((103, 6.0))
        sources[1][1].gateway.payload[103] = b"own-new-103"
        mux.playlist("tv", sources, now=112)
        self.assertEqual(mux.source_slot, 2)
        self.assertEqual(mux.switches, 0)
        self.assertEqual(mux.history[-1].content, b"own-new-103")

    def test_failover_keeps_monotonic_sequence_and_discontinuity(self):
        mux = FailoverMux()
        original = FakeStream("CDN-1")
        backup = FakeStream("CDN-2")
        third = FakeStream("CDN-3")
        candidates = [(1, original), (2, backup), (3, third)]
        playlist = mux.playlist("safe-token", candidates, now=100).decode()
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", playlist)
        self.assertIn(f"/safe-token/segment/{mux.generation}.1.ts", playlist)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n", playlist)
        self.assertEqual(mux.source_slot, 1)
        self.assertEqual(mux.segment(1), b"CDN-1-100")
        self.assertEqual(mux.segment(3), b"CDN-1-102")

        # CDN 1 stops while CDN 2 remains fresh. The TV keeps a stable URL
        # and gets an explicit decoder discontinuity, not reused source IDs.
        original.gateway.fresh = False
        swapped = mux.playlist("safe-token", candidates, now=120).decode()
        self.assertEqual(mux.source_slot, 2)
        self.assertEqual(mux.switches, 1)
        self.assertIn("#EXT-X-DISCONTINUITY\n", swapped)
        self.assertIn(f"/safe-token/segment/{mux.generation}.4.ts", swapped)
        self.assertEqual(mux.segment(1), b"CDN-1-100")
        self.assertEqual(mux.segment(4), b"CDN-2-100")
        self.assertEqual(mux.segment_record(1).source_slot, 1)
        self.assertEqual(mux.segment_record(4).source_slot, 2)
        self.assertEqual(mux.segment(6), b"CDN-2-102")

        # A fresh primary should not trigger instant source ping-pong.
        original.gateway.fresh = True
        self.assertEqual(mux.source_slot, 2)
        mux.playlist("safe-token", candidates, now=135)
        self.assertEqual(mux.source_slot, 2)  # Still within switch cooldown.
        self.assertNotIn("provider", json.dumps(mux.status()))

    def test_rollover_preserves_discontinuity_sequence_after_expiring_old_segments(self):
        mux = FailoverMux()
        a = FakeStream("A")
        b = FakeStream("B")
        candidates = [(1, a), (2, b)]
        mux.playlist("private", candidates, now=100)
        a.gateway.fresh = False
        mux.playlist("private", candidates, now=120)
        # Publish enough new B content to evict the boundary segment.
        for last in range(103, 119):
            b.gateway.segments = [(last, 6.0)]
            b.gateway.payload[last] = f"B-{last}".encode()
            text = mux.playlist("private", candidates, now=126 + 6 * (last - 103)).decode()
        self.assertEqual(mux.discontinuity_sequence, 1)
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:1", text)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n", text)
        self.assertIsNone(mux.segment(1))
        self.assertLessEqual(len(mux.history), 12)

    def test_ready_downloader_but_frozen_tv_playlist_uses_healthy_backup(self):
        """CDN still fetches new fragments while published TV HLS is stuck."""
        mux = FailoverMux()
        primary = FakeStream("primary")
        backup = FakeStream("backup")
        mux.playlist("tv-token", [(1, primary), (2, backup)], now=100)
        self.assertEqual(mux.source_slot, 1)
        # The provider download remains fresh but gateway.published()
        # keeps offering segment 102, even after upstream reached 122.
        primary.gateway.payload[122] = b"downloaded-not-published"
        primary.gateway.fresh = True
        primary.gateway.segments = [(100, 6.0), (101, 6.0), (102, 6.0)]
        mux.playlist("tv-token", [(1, primary), (2, backup)], now=110)
        self.assertEqual(mux.source_slot, 1)
        new_manifest = mux.playlist("tv-token", [(1, primary), (2, backup)], now=120)
        self.assertEqual(mux.source_slot, 2)
        self.assertEqual(mux.last_progress, 120)
        self.assertIn(b"#EXT-X-DISCONTINUITY", new_manifest)
        self.assertEqual(mux.segment(4), b"backup-100")
        self.assertEqual(mux.switches, 1)

    def test_new_viewer_after_idle_never_gets_last_sessions_video(self):
        mux = FailoverMux()
        old = FakeStream("previous")
        first = mux.playlist("tv-token", [(1, old)], now=100)
        self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:1", first)
        old_last = mux.next_sequence
        expired = FakeStream("new")
        expired.gateway.fresh = False
        # Both upstreams expired while no TV was polling the channel.
        self.assertIsNone(mux.playlist("tv-token", [(1, expired)], now=160))
        self.assertIsNone(mux.segment(1))
        self.assertEqual(mux.next_sequence, old_last)
        self.assertIsNone(mux.source_slot)
        # Fresh video after warmup has the same TV token but never rewinds IDs.
        expired.gateway.fresh = True
        expired.gateway.segments = [(200, 6.0), (201, 6.0), (202, 6.0)]
        expired.gateway.payload = {seq: b"new-video" for seq, _ in expired.gateway.segments}
        new = mux.playlist("tv-token", [(1, expired)], now=165)
        self.assertIn(f"#EXT-X-MEDIA-SEQUENCE:{old_last}".encode(), new)
        self.assertEqual(mux.segment(old_last), b"new-video")
        self.assertNotIn(b"previous", new)

    def test_idle_resume_with_same_worker_requires_a_new_source_segment(self):
        mux = FailoverMux()
        gateway = FakeStream("old")
        mux.playlist("tv", [(1, gateway)], now=100)
        self.assertEqual(mux.segment(1), b"old-100")
        # Same worker remains nominally READY but still advertises 100..102.
        self.assertIsNone(mux.playlist("tv", [(1, gateway)], now=160))
        self.assertIsNone(mux.segment(1))
        self.assertEqual(mux.next_sequence, 4)
        gateway.gateway.segments.append((103, 6.0))
        gateway.gateway.payload[103] = b"fresh-103"
        manifest = mux.playlist("tv", [(1, gateway)], now=165)
        self.assertIn(b"#EXT-X-MEDIA-SEQUENCE:4", manifest)
        self.assertEqual(mux.segment(4), b"fresh-103")
        self.assertNotIn(b"old-100", [value.content for value in mux.history])

    def test_two_stalled_workers_cannot_republish_old_windows(self):
        mux = FailoverMux()
        a, b, c = [FakeStream(label) for label in ("A", "B", "C")]
        sources = [(1, a), (2, b), (3, c)]
        mux.playlist("tv", sources, now=100)
        mux.playlist("tv", sources, now=120)
        self.assertEqual(mux.source_slot, 2)
        c.gateway.segments.append((103, 6.0))
        c.gateway.payload[103] = b"C-103"
        mux.playlist("tv", sources, now=140)
        self.assertEqual(mux.source_slot, 3)
        self.assertEqual(mux.history[-1].content, b"C-103")
        # A recovered backup may return, but only its unseen video is added.
        a.gateway.segments.append((103, 6.0))
        a.gateway.payload[103] = b"A-103"
        mux.playlist("tv", sources, now=160)
        self.assertEqual(mux.source_slot, 1)
        self.assertEqual(mux.next_sequence, 11)
        self.assertEqual(mux.segment(10), b"A-103")
        self.assertTrue(mux.history[-1].discontinuity)

    def test_cdn_monitor_shows_switch_reason_liveness_and_no_secrets(self):
        mux = FailoverMux()
        first = FakeStream("primary")
        backup = FakeStream("backup")
        manifest = mux.playlist("SECRET_TV_TOKEN", [(1, first), (2, backup)], now=100)
        self.assertIsNotNone(manifest)
        first.gateway.fresh = False
        mux.playlist("SECRET_TV_TOKEN", [(1, first), (2, backup)], now=120)
        data = mux.status()
        self.assertEqual(data["selected_cabinet"], 2)
        self.assertEqual(data["switches"], 1)
        self.assertEqual(data["virtual_segment_sequence"], 6)
        self.assertEqual(data["cached_video_seconds"], 36.0)
        self.assertEqual(data["switch_history"][0]["reason"], "SOURCE_UNAVAILABLE")
        self.assertEqual(data["switch_history"][0]["from_cabinet"], 1)
        self.assertEqual(data["switch_history"][0]["to_cabinet"], 2)
        self.assertEqual(data["switch_history"][1]["reason"], "START")
        self.assertIn("published_age_seconds", data["sources"][0])
        self.assertIn("downloaded_sequence", data["sources"][0])
        self.assertNotIn("SECRET_TV_TOKEN", json.dumps(data))
        self.assertNotIn("provider", json.dumps(data))

    def test_cdn_monitor_event_log_is_bounded(self):
        mux = FailoverMux()
        a, b = FakeStream("A"), FakeStream("B")
        sources = [(1, a), (2, b)]
        mux.playlist("tv", sources, now=100)
        for n in range(25):
            current = a if n % 2 else b
            current.gateway.segments.append((103+n, 6.0))
            current.gateway.payload[103+n] = b"fresh-video"
            # The current source is frozen but an alternate keeps advancing.
            mux.last_progress -= 30
            mux.last_switch -= 30
            mux.playlist("tv", sources, now=120+n*20)
        status = mux.status()
        self.assertLessEqual(len(status["switch_history"]), 20)
        self.assertEqual(len(status["switch_history"]), 20)

    def test_buffer_keeps_advancing_during_outage_without_backup(self):
        mux = FailoverMux()
        stream = FakeStream("primary")
        gw = stream.gateway = Gateway("https://example.test/live", delay=90)
        for sequence in range(100, 118):
            gw.cache[sequence] = (6.0, str(sequence).encode())
        gw.latest = 117
        gw.last_progress = gw.last_segment_download = 100.0
        with patch("time.time") as wall, patch("time.monotonic") as clock:
            for now in range(100, 191, 6):
                wall.return_value = clock.return_value = now
                mux.playlist("tv", [(1, stream)], now=now)
        self.assertFalse(mux.source_health[1]["fresh"])
        self.assertEqual(mux.segment(18), b"117")

    def test_idle_resume_tries_backup_in_same_poll_when_primary_repeats(self):
        mux = FailoverMux()
        a, b = FakeStream("A"), FakeStream("B")
        mux.playlist("tv", [(1, a)], now=100)
        manifest = mux.playlist("tv", [(1, a), (2, b)], now=160)
        self.assertIsNotNone(manifest)
        self.assertEqual(mux.source_slot, 2)
        self.assertIsNone(mux.segment(1))
        self.assertEqual(mux.segment(4), b"B-100")

    def test_new_worker_in_stalled_slot_can_publish_overlapping_sequence(self):
        mux = FailoverMux()
        a, replacement = FakeStream("A"), FakeStream("replacement")
        mux.playlist("tv", [(1, a)], now=100)
        mux.playlist("tv", [(1, a)], now=120)
        mux.playlist("tv", [(1, replacement)], now=121)
        self.assertEqual(mux.segment(4), b"replacement-100")
        self.assertTrue(mux.history[3].discontinuity)

    def test_source_rotation_with_same_worker_and_sequence_starts_new_epoch(self):
        mux = FailoverMux()
        first, second = "https://a.example/live", "https://b.example/live"
        stream = FakeStream("primary")
        gateway = stream.gateway = Gateway(first, delay=0, source_refresher=lambda: second)
        manifest = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n" + "".join(
            f"#EXTINF:6,\n{i}.ts\n" for i in range(100, 103))
        for now, url, video in [(100, first, b"A"), (106, second, b"B")]:
            if url == second:
                self.assertTrue(gateway.refresh_source())
            with patch("iptv_gateway.media_manifest", return_value=(manifest, url)), \
                 patch("iptv_gateway.fetch_segment", return_value=video):
                gateway.cycle()
            mux.playlist("tv", [(1, stream)], now=now)
        self.assertEqual(mux.segment(3), b"A")
        self.assertEqual(mux.segment(4), b"B")
        self.assertTrue(mux.history[3].discontinuity)
        self.assertEqual(mux.switches, 0)

    def test_no_switch_when_backup_is_stale(self):
        mux = FailoverMux()
        a = FakeStream("A")
        b = FakeStream("B")
        mux.playlist("private", [(1, a), (2, b)], now=100)
        a.gateway.fresh = False
        b.gateway.fresh = False
        playlist = mux.playlist("private", [(1, a), (2, b)], now=140)
        self.assertEqual(mux.source_slot, 1)
        self.assertEqual(mux.switches, 0)
        self.assertIsNotNone(playlist)

    def test_backup_never_advances_gateway_playback_until_selected(self):
        mux = FailoverMux()
        a = FakeStream("A")
        b = FakeStream("B")
        calls = []
        def record_publish():
            calls.append("backup")
            return list(b.gateway.segments)
        b.gateway.published = record_publish
        mux.playlist("private", [(1, a), (2, b)], now=100)
        self.assertEqual(calls, [])
        a.gateway.fresh = False
        mux.playlist("private", [(1, a), (2, b)], now=120)
        self.assertEqual(calls, ["backup"])

    def test_late_source_join_keeps_virtual_segment_ids(self):
        mux = FailoverMux()
        a = FakeStream("A")
        b = FakeStream("B")
        mux.playlist("private", [(1, a), (2, b)], now=100)
        first_sequence = mux.next_sequence
        # A new cabinet was configured after the channel started.
        c = FakeStream("C")
        mux.playlist("private", [(1, a), (2, b), (3, c)], now=110)
        self.assertEqual(mux.next_sequence, first_sequence)


if __name__ == "__main__":
    unittest.main()
