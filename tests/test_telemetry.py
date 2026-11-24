"""Persistent IPTV usage and incident telemetry tests (offline, no provider)."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import iptv_gateway
import iptv_multi
from iptv_telemetry import Telemetry, query_incidents, query_viewing


class TelemetryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "usage.sqlite3"
        self.store = Telemetry(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_sessions_track_wall_time_not_segment_download_bursts(self):
        ts = int(time.time()) - 200
        device = self.store.viewer("10.0.0.1", "TV A")
        self.assertEqual(device, self.store.viewer("10.0.0.1", "TV A"))
        self.assertNotEqual(device, self.store.viewer("10.0.0.2", "TV A"))
        # Burst at startup: no invented minutes from downloading 12 segments.
        for _ in range(12):
            self.store.observe_segment("channel-a", device, ts)
        self.store.observe_segment("channel-a", device, ts + 10)
        self.store.observe_segment("channel-a", device, ts + 20)
        self.store.observe_segment("channel-a", device, ts + 70)  # gap capped at 30s
        self.store.observe_segment("channel-a", device, ts + 200)  # new session
        self.store.observe_segment("channel-b", device, ts + 200)
        self.store.flush()
        data = query_viewing(self.path, 1)
        by_name = {entry["channel_id"]: entry for entry in data["channels"]}
        self.assertEqual(data["total_sessions"], 3)
        self.assertEqual(by_name["channel-a"]["seconds"], 50)
        self.assertEqual(by_name["channel-b"]["seconds"], 0)
        self.assertEqual(data["total_seconds"], 50)
        # The database never contains raw IP or user agent strings.
        self.assertNotIn(b"10.0.0.1", self.path.read_bytes())
        self.assertNotIn(b"TV A", self.path.read_bytes())

    def test_history_pages_are_per_cabinet_and_include_low_ranked_channels(self):
        ts = int(time.time()) - 300
        for i in range(25):
            for prefix in ("", "p2:"):
                self.store.observe_segment(f"{prefix}ch{i}", "tv", ts + i)
        self.store.flush()
        page = query_viewing(self.path, 1, slot=2, rank_page=1, recent_page=2)
        self.assertEqual((page["unique_channels"], page["total_sessions"]), (25, 25))
        self.assertEqual([r["channel_id"] for r in page["channels"]],
                         [f"p2:ch{i}" for i in (4, 3, 2, 1, 0)])
        self.assertEqual([r["channel_id"] for r in page["sessions"]], ["p2:ch0"])

    def test_incidents_filter_and_no_secret_payloads(self):
        ts = int(time.time()) - 10
        self.store.incident("channel-a", "upstream_error", "MANIFEST_HTTP_404", timestamp=ts)
        self.store.incident("channel-a", "stream_stalled", "UPSTREAM_NO_PROGRESS",
                            duration_seconds=41.4, timestamp=ts + 1)
        self.store.incident("channel-a", "stream_recovered", "RESTORED",
                            duration_seconds=47, timestamp=ts + 2)
        self.store.incident("channel-a", "client_error", "HTTP_404_SEGMENT", timestamp=ts + 3)
        self.store.flush()
        data = query_incidents(self.path, 1)
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["summary"]["stream_stalled"], 1)
        stalls = query_incidents(self.path, 1, "stream_stalled")
        self.assertEqual(stalls["total"], 1)
        self.assertEqual(stalls["events"][0]["duration_seconds"], 41.4)
        with self.assertRaises(ValueError):
            query_incidents(self.path, 90)

    def test_existing_data_survives_restarting_writer(self):
        self.store.observe_segment("channel-a", "anonymized", time.time()-12)
        self.store.close()
        self.store = Telemetry(self.path)
        self.store.observe_segment("channel-a", "anonymized", time.time())
        self.store.flush()
        self.assertEqual(query_viewing(self.path, 1)["total_sessions"], 1)

    def test_upstream_stall_emits_only_one_event_until_recovery(self):
        events = []
        gw = iptv_gateway.Gateway("https://source.invalid/stream.m3u8",
                                  on_event=lambda kind, label, seconds:
                                  events.append((kind, label, seconds)))
        gw.last_progress = 100
        gw.target = 10
        with patch.object(iptv_gateway.time, "time", return_value=141):
            gw.check_stall()
        with patch.object(iptv_gateway.time, "time", return_value=145):
            gw.check_stall()
        gw.last_progress = 148
        with patch.object(iptv_gateway.time, "time", return_value=149):
            gw.check_stall()
        self.assertEqual([e[0] for e in events],
                         ["stream_stalled", "stream_recovered"])
        self.assertEqual(events[0][2], 41)
        self.assertEqual(events[1][2], 49)

    def test_segment_download_freeze_even_when_playlist_progresses(self):
        events = []
        gw = iptv_gateway.Gateway("https://source.invalid",
                                  on_event=lambda kind, label, seconds:
                                  events.append((kind, label, seconds)))
        gw.last_progress = 139  # playlist still changing
        gw.last_segment_download = 101  # but video data no longer downloads
        gw.target = 10
        with patch.object(iptv_gateway.time, "time", return_value=141):
            gw.check_stall()
        self.assertEqual(events[0][0], "stream_stalled")
        self.assertEqual(events[0][1], "NO_DOWNLOADED_SEGMENTS")
        gw.last_segment_download = 145
        gw.last_progress = 145
        with patch.object(iptv_gateway.time, "time", return_value=146):
            gw.check_stall()
        self.assertEqual(events[1][0], "stream_recovered")

    def test_safe_failure_callback(self):
        failures = []
        gw = iptv_gateway.Gateway("https://source.invalid",
                                  on_event=lambda kind, detail, elapsed:
                                  failures.append((kind, detail)))
        gw.record_upstream_failure("MANIFEST",
                                   urllib.error.HTTPError("https://source.invalid/TOKEN", 404, "oops", {}, None))
        self.assertEqual(failures[0][0], "upstream_error")
        self.assertNotIn("TOKEN", failures[0][1])


class MultiChannelUsageTests(unittest.TestCase):
    def test_successful_segment_get_is_viewing_but_head_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Telemetry(Path(folder) / "watch.sqlite3")
            channel = iptv_multi.channels_from_entries([
                ("TV", "Demo", "https://provider.invalid/private/channel")])[0]
            manager = iptv_multi.MultiGateway([channel], Path(folder) / "private.m3u",
                                              max_streams=1, telemetry=store)
            with patch.object(iptv_multi.Gateway, "run", lambda g: g.stop.wait(3)):
                worker = manager.start(channel.id)
                worker.gateway.cache[15] = (10.0, b"ABCDEFGHIJK")
                worker.gateway.total_bytes = 11
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    iptv_multi.make_multi_handler(manager, "private-tv-key-123456"))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}/private-tv-key-123456/channel/{channel.id}/segment/15.ts"
                    req = urllib.request.Request(url, method="HEAD")
                    with urllib.request.urlopen(req, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                    store.flush()
                    self.assertEqual(query_viewing(store.path, 1)["total_sessions"], 0)
                    with urllib.request.urlopen(url, timeout=3) as response:
                        self.assertEqual(response.read(), b"ABCDEFGHIJK")
                    with self.assertRaises(urllib.error.HTTPError) as response:
                        urllib.request.urlopen(url.replace("15.ts", "19.ts"), timeout=3)
                    self.assertEqual(response.exception.code, 404)
                    store.flush()
                    history = query_viewing(store.path, 1)
                    incidents = query_incidents(store.path, 1)
                    self.assertEqual(history["total_sessions"], 1)
                    self.assertEqual(history["channels"][0]["channel_id"], channel.id)
                    self.assertEqual(incidents["summary"]["client_error"], 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)
                    manager.close()
                    store.close()


if __name__ == "__main__":
    unittest.main()
