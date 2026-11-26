import tempfile
import time
import unittest
from pathlib import Path

import iptv_telemetry as telemetry


class StabilityTests(unittest.TestCase):
    def test_concurrent_delivery_errors_reserve_and_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            self.addCleanup(store.close)
            t = int(time.time()) - 80
            def video(channel, viewer, offset, seq, source):
                store.observe_segment(channel, viewer, t + offset, piece=str(seq), source=source)
            def fail(channel, viewer, offset):
                store.incident(channel, "client_error", "HTTP_404_SEGMENT_CACHE_MISS",
                               timestamp=t + offset, diagnostics={"client": viewer})
            video("a", "tv", 1, 1, 2)
            video("p2:a", "tv", 2, 1, 2)
            store.incident("a", "upstream_error", "MANIFEST_HTTP_404", timestamp=t + 3)
            fail("a", "tv", 5)
            fail("a", "tv", 6)
            video("a", "tv", 7, 1, 2)  # Retry of the previous fragment.
            video("a", "other", 8, 12, 1)
            store.flush()
            data = telemetry.query_stability(path, 1)
            live = {(r["channel_id"], r["viewer"]): r for r in data["live"]}
            self.assertEqual(live[("a", "tv")]["state"], "interrupted")
            self.assertEqual(live[("p2:a", "tv")]["state"], "playing")
            self.assertEqual(live[("a", "other")]["state"], "playing")
            self.assertEqual(sum(r["errors"] for r in data["channels"]), 2)
            video("a", "tv", 11, 2, 2)
            store.flush()
            result = telemetry.query_stability(path, 1)
            self.assertEqual(result["episodes"][0]["recovered"], t + 11)
            self.assertEqual(result["episodes"][0]["started"], t + 5)
            self.assertEqual(result["episodes"][0]["recovery_slot"], 2)
            self.assertEqual(next(r for r in result["live"] if r["channel_id"] == "a"
                                  and r["viewer"] == "tv")["state"], "backup")

    def test_stale_manifest_needs_recent_requests_and_prior_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tv.db"
            store = telemetry.Telemetry(path)
            self.addCleanup(store.close)
            t = int(time.time()) - 240
            store.observe_segment("a", "tv", t, piece="1", source=1)
            for offset in (1, 11, 22, 35, 66):
                store.observe_manifest("a", "tv", "4", 10, t + offset, window_seconds=60)
                if offset == 35:
                    store.flush()
                    self.assertEqual(telemetry.query_stability(path)["episodes"], [])
                store.observe_manifest("a", "probe", "4", 10, t + offset)
            store.observe_segment("a", "tv", t + 235, piece="2", source=1)
            store.flush()
            result = telemetry.query_stability(path, 1)
            self.assertEqual(len(result["episodes"]), 1)
            self.assertEqual(result["episodes"][0]["reason"], "playlist")
            self.assertIsNone(result["episodes"][0]["recovered"])
            self.assertEqual(len(result["live"]), 1)
            self.assertEqual(result["live"][0]["state"], "playing")
