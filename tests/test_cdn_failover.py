"""Three-CDN VF pilot: no private URLs, no cross-account paths, no HLS mixing."""
import json
import unittest

from iptv_cdn_failover import FailoverMux, is_pilot


class FakeGateway:
    def __init__(self, label):
        self.label = label
        self.fresh = True
        self.segments = [(100, 6.0), (101, 6.0), (102, 6.0)]
        self.payload = {seq: f"{label}-{seq}".encode() for seq, _ in self.segments}

    def published(self):
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
    def test_pilot_is_exact_and_case_insensitive(self):
        self.assertTrue(is_pilot("  VF  Теория большого взрыва  "))
        self.assertFalse(is_pilot("Теория большого взрыва"))
        self.assertFalse(is_pilot("VF Теория большого взрыва 2"))
        self.assertFalse(is_pilot("VF Кинокомедия HD"))

    def test_failover_keeps_monotonic_sequence_and_discontinuity(self):
        mux = FailoverMux()
        original = FakeStream("CDN-1")
        backup = FakeStream("CDN-2")
        third = FakeStream("CDN-3")
        candidates = [(1, original), (2, backup), (3, third)]
        playlist = mux.playlist("safe-token", candidates, now=100).decode()
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", playlist)
        self.assertIn("/safe-token/segment/1.ts", playlist)
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
        self.assertIn("/safe-token/segment/4.ts", swapped)
        self.assertEqual(mux.segment(1), b"CDN-1-100")
        self.assertEqual(mux.segment(4), b"CDN-2-100")
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
