"""Offline tests for the on-demand family TV catalog prototype."""
import http.client
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import iptv_multi as multi


class MultiChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "private.m3u"
        self.entries = [
            ("Channel Alpha", "Tests", "http://upstream.test/PRIVATE_TOKEN/alpha"),
            ("Channel Beta", "Tests", "http://upstream.test/PRIVATE_TOKEN/beta"),
        ]
        self.channels = multi.channels_from_entries(self.entries)
        # Workers should never touch actual network resources in a unit test.
        self.worker_patch = patch.object(multi.Gateway, "run",
                                         lambda gw: gw.stop.wait(10))
        self.worker_patch.start()
        self.manager = multi.MultiGateway(self.channels, self.path, delay=30,
                                          max_streams=2, cache_mib=64,
                                          idle_seconds=20, startup_wait=0)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                    multi.make_multi_handler(self.manager, "test-token-1234567890"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/test-token-1234567890"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.manager.close()
        self.worker_patch.stop()
        self.temp.cleanup()

    def request(self, path):
        with urllib.request.urlopen(self.url + path, timeout=3) as resp:
            return resp.status, resp.read()

    def test_catalog_contains_multiple_channels_without_origin_url(self):
        status, body = self.request("/channels.m3u")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertEqual(text.count("#EXTINF:"), 2)
        for item in self.channels:
            self.assertIn(item.name, text)
            self.assertIn(f"/channel/{item.id}/index.m3u8", text)
        self.assertNotIn("PRIVATE_TOKEN", text)
        self.assertNotIn("upstream.test", text)
        self.assertEqual(len(self.manager.active), 0,
                         "Browsing the catalog must not start every upstream.")

    def test_channel_ids_stay_same_when_private_m3u_urls_rotate(self):
        rotated = [(name, group, url + "/rotated") for name, group, url in self.entries]
        self.assertEqual([c.id for c in self.channels],
                         [c.id for c in multi.channels_from_entries(rotated)])
        with_duplicates = multi.channels_from_entries(
            self.entries + [self.entries[0]])
        self.assertNotEqual(with_duplicates[0].id, with_duplicates[-1].id)

    def test_on_demand_channel_is_warmed_only_when_requested(self):
        first = self.channels[0]
        with self.assertRaises(urllib.error.HTTPError) as warming:
            self.request(f"/channel/{first.id}/index.m3u8")
        self.assertEqual(warming.exception.code, 503)
        self.assertEqual(len(self.manager.active), 1)
        self.assertIn(first.id, self.manager.active)
        self.assertNotIn(self.channels[1].id, self.manager.active)
        gw = self.manager.active[first.id].gateway
        with gw.lock:
            for seq in range(10, 24):
                gw.cache[seq] = (10.0, b"0123456789")
                gw.total_bytes += 10
            gw.latest = 23
        status, body = self.request(f"/channel/{first.id}/index.m3u8")
        self.assertEqual(status, 200)
        data = body.decode()
        self.assertIn(f"/test-token-1234567890/channel/{first.id}/segment/", data)
        self.assertNotIn("/test-token-1234567890/segment/", data)
        self.assertNotIn("PRIVATE_TOKEN", data)
        published_seq = gw.last_published_seq
        status, video = self.request(f"/channel/{first.id}/segment/{published_seq}.ts")
        self.assertEqual((status, video), (200, b"0123456789"))
        with urllib.request.urlopen(urllib.request.Request(
                self.url + f"/channel/{first.id}/segment/{published_seq}.ts",
                headers={"Range": "bytes=2-5"})) as response:
            self.assertEqual((response.status, response.read()), (206, b"2345"))
        metrics = json.loads(self.request("/status")[1])
        self.assertEqual(metrics["active_streams"], 1)
        stream = metrics["streams"][0]
        self.assertEqual(stream["client_manifest_200"], 1)
        self.assertEqual(stream["client_segment_200"], 1)
        self.assertEqual(stream.get("client_segment_206"), 1)

    def test_existing_single_channel_tv_urls_still_play_after_upgrade(self):
        # This simulates the TV app caching its old /TOKEN/index.m3u8 URL
        # instead of fetching the new multi-channel catalog on restart.
        first = self.channels[0]
        self.manager.legacy_channel_id = first.id
        stream = self.manager.start(first.id, pinned=True)
        gw = stream.gateway
        with gw.lock:
            for seq in range(10, 24):
                gw.cache[seq] = (10.0, b"0123456789")
                gw.total_bytes += 10
            gw.latest = 23
        code, manifest = self.request("/index.m3u8")
        self.assertEqual(code, 200)
        self.assertIn(f"/channel/{first.id}/segment/", manifest.decode())
        seq = gw.last_published_seq
        code, content = self.request(f"/segment/{seq}.ts")
        self.assertEqual((code, content), (200, b"0123456789"))
        # Existing Range-requesting TV players must also remain compatible.
        url = self.url + f"/segment/{seq}.ts"
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"Range": "bytes=2-5"})) as response:
            self.assertEqual((response.status, response.read()), (206, b"2345"))
        data = json.loads(self.request("/status")[1])
        self.assertEqual(data["legacy_channel"], first.name)
        self.assertEqual(data["legacy_manifest_requests"], 1)
        self.assertEqual(data["legacy_segment_requests"], 2)
        self.assertEqual(data["streams"][0]["client_manifest_200"], 1)
        self.assertEqual(data["streams"][0]["client_segment_200"], 1)
        # Catalog still contains all channels and does not leak origin URLs.
        self.request("/channels.m3u")
        self.assertEqual(json.loads(self.request("/status")[1])["catalog_requests"], 1)
        self.assertEqual(len(self.manager.active), 1)

    def test_legacy_urls_are_not_enabled_without_explicit_prewarm(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/index.m3u8")
        self.assertEqual(ctx.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/segment/100.ts")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(len(self.manager.active), 0)

    def test_cold_channel_waits_for_video_and_returns_200_on_first_request(self):
        self.manager.startup_wait = 2.0
        first = self.channels[0]
        result, error = [], []
        def open_cold_channel():
            try:
                result.append(self.request(f"/channel/{first.id}/index.m3u8"))
            except Exception as exc:
                error.append(exc)
        reader = threading.Thread(target=open_cold_channel, daemon=True)
        reader.start()
        deadline = time.monotonic() + 2
        while first.id not in self.manager.active and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(first.id, self.manager.active)
        self.assertTrue(reader.is_alive(), "cold manifest request must still be waiting")
        # Test the source producing enough complete segments *after*
        # the TV's original GET, before the timeout expires.
        gw = self.manager.active[first.id].gateway
        with gw.lock:
            for seq in range(10, 24):
                gw.cache[seq] = (10.0, b"0123456789")
                gw.total_bytes += 10
            gw.latest = 23
        reader.join(timeout=3)
        self.assertFalse(reader.is_alive())
        self.assertFalse(error, repr(error))
        self.assertEqual(result[0][0], 200)
        self.assertIn(f"/channel/{first.id}/segment/", result[0][1].decode())
        metrics = json.loads(self.request("/status")[1])
        self.assertEqual(metrics["warmup_successes"], 1)
        self.assertEqual(metrics["warmup_timeouts"], 0)
        self.assertEqual(metrics["streams"][0]["client_manifest_503"], 0)
        self.assertEqual(metrics["streams"][0]["pending_requests"], 0)

    def test_cold_channel_timeout_exposes_manifest_503_and_warmup_count(self):
        self.manager.startup_wait = 0.1
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request(f"/channel/{self.channels[0].id}/index.m3u8")
        self.assertEqual(ctx.exception.code, 503)
        metrics = json.loads(self.request("/status")[1])
        self.assertEqual(metrics["warmup_timeouts"], 1)
        self.assertEqual(metrics["streams"][0]["client_manifest_503"], 1)

    def test_cold_channel_starts_with_reserve_and_keeps_playing_through_refill(self):
        # 10s is now explicit fast mode; the safe default is 30s.
        self.manager.delay = 90
        self.manager.startup_reserve = 10
        cid = self.channels[0].id
        gw = self.manager.start(cid).gateway
        path = f"/channel/{cid}/index.m3u8"

        def cache(seq):
            gw.cache[seq] = (10.0, b"segment")
            gw.latest = seq
            gw.last_advance_at = time.monotonic()

        with patch.object(time, "monotonic", return_value=100.0) as clock:
            for seq in range(100, 103):
                cache(seq)
            self.assertFalse(json.loads(gw.status())["ready"])
            cache(103)
            self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(gw.last_published_seq, 102)
            # No new downloads for one segment duration.
            clock.return_value = 110.0
            self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(gw.last_published_seq, 103)
            # Catch-up downloads add headroom without stopping the TV cursor.
            for seq in range(104, 110):
                cache(seq)
            for seq in range(104, 108):
                clock.return_value += 10
                cache(seq + 6)
                self.assertEqual(self.request(path)[0], 200)
                self.assertEqual(gw.last_published_seq, seq)
                self.assertEqual(json.loads(gw.status())["effective_buffer_delay_seconds"], 60)

    def test_default_reserve_reduces_freeze_risk_and_can_be_capped(self):
        self.manager.delay = 90
        self.assertEqual(self.manager.startup_reserve, 30)
        cid = self.channels[0].id
        gw = self.manager.start(cid).gateway
        self.assertEqual(gw.startup_reserve, 30)
        with patch.object(time, "monotonic", return_value=100.0):
            with gw.lock:
                for seq in range(100, 105):
                    gw.cache[seq] = (10.0, b"data")
                gw.latest = 104
                gw.last_advance_at = 1.0  # Upstream idle must not count as buffer.
            self.assertFalse(json.loads(gw.status())["ready"])
            with gw.lock:
                gw.cache[105] = (10.0, b"data")
                gw.latest = 105
            status = json.loads(gw.status())
            self.assertTrue(status["ready"])
            self.assertEqual(status["startup_reserve_seconds"], 30)
            self.assertEqual(status["effective_buffer_delay_seconds"], 30)
            self.assertEqual(gw.published()[-1][0], 102)
        self.manager.delay = 15
        self.manager.startup_reserve = 30
        other = self.manager.start(self.channels[1].id).gateway
        self.assertEqual(other.startup_reserve, 15)

    def test_client_segment_misses_show_cache_bounds_at_request_time(self):
        cid = self.channels[0].id
        gw = self.manager.start(cid).gateway
        gw.cache[20] = (10, b"a")
        gw.cache[22] = (10, b"b")
        for seq in (19, 21, 23):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(f"/channel/{cid}/segment/{seq}.ts")
            self.assertEqual(caught.exception.code, 404)
        gw.cache[23] = (10, b"c")
        stream = json.loads(self.request("/status")[1])["streams"][0]
        misses = stream.get("recent_client_segment_404", [])
        self.assertEqual([(m["sequence"], m["cache_first"], m["cache_last"]) for m in misses],
                         [(19, 20, 22), (21, 20, 22), (23, 20, 22)])
        self.assertTrue(all(m["utc_unix"] > 0 for m in misses))

    def test_switch_reuses_only_inactive_stream_slot(self):
        first, second = self.channels
        self.manager.idle_seconds = 90
        self.assertIsNotNone(self.manager.start(first.id))
        self.assertIsNotNone(self.manager.start(second.id))
        third = multi.channels_from_entries([("Third", "", "https://example.test/3")])[0]
        self.manager.channels[third.id] = third
        self.assertIsNone(self.manager.start(third.id))
        self.assertEqual(self.manager.stream_limit_denials, 1)
        first_stream = self.manager.active[first.id]
        first_stream.accessed -= 25
        # Do not reclaim a stream while its first manifest GET is waiting.
        first_stream.pending_requests = 1
        self.assertIsNone(self.manager.start(third.id))
        first_stream.pending_requests = 0
        self.assertIsNotNone(self.manager.start(third.id))
        self.assertNotIn(first.id, self.manager.active)
        self.assertIn(second.id, self.manager.active)
        self.assertEqual(self.manager.recycled_idle_streams, 1)
        self.assertEqual(self.manager.stream_limit_denials, 2)

    def test_active_and_prewarmed_streams_are_not_evicted_on_switch(self):
        first, second = self.channels
        self.manager.idle_seconds = 90
        self.assertIsNotNone(self.manager.start(first.id, pinned=True))
        self.assertIsNotNone(self.manager.start(second.id))
        third = multi.channels_from_entries([("Third", "", "https://example.test/3")])[0]
        self.manager.channels[third.id] = third
        self.manager.active[first.id].accessed -= 50
        self.assertIsNone(self.manager.start(third.id))
        self.manager.active[second.id].accessed -= 25
        self.assertIsNotNone(self.manager.start(third.id))
        self.assertIn(first.id, self.manager.active)
        self.assertNotIn(second.id, self.manager.active)

    def test_bad_channel_or_token_is_not_a_public_proxy(self):
        for path in ("/channel/invalid/index.m3u8",
                     f"/channel/{self.channels[0].id}/anything"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request(path)
            self.assertEqual(ctx.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url.replace("test-token-1234567890",
                                                    "wrong-token-1234567890") + "/status")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(len(self.manager.active), 0)

    def test_limit_and_idle_cleanup(self):
        first, second = self.channels
        self.assertIsNotNone(self.manager.start(first.id))
        self.assertIsNotNone(self.manager.start(second.id))
        self.assertEqual(len(self.manager.active), 2)
        # The cap is strict. Never open excess provider streams accidentally.
        another = multi.channels_from_entries([("Third", "", "https://example.test/3")])[0]
        self.manager.channels[another.id] = another
        self.assertIsNone(self.manager.start(another.id))
        self.manager.active[first.id].accessed -= 30
        self.manager.sweep()
        self.assertEqual(len(self.manager.active), 1)
        self.assertIsNotNone(self.manager.start(another.id))

    def test_pinned_stream_is_not_evicted(self):
        first = self.channels[0]
        stream = self.manager.start(first.id, pinned=True)
        stream.accessed -= 1000
        self.manager.sweep()
        self.assertIn(first.id, self.manager.active)


class MultiChannelStartupTests(unittest.TestCase):
    def test_remote_failure_falls_back_to_local_catalog(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "local.m3u"
            path.write_text("#EXTM3U\n#EXTINF:-1,Local\nhttps://example.test/local\n", encoding="utf-8")
            args = ["--playlist", str(path), "--playlist-url-env", "TEST_PLAYLIST",
                    "--prewarm", "Local", "--token-file", str(path.with_suffix(".token"))]
            for failure in (http.client.IncompleteRead(b""), "<html>Maintenance</html>"):
                with self.subTest(failure=type(failure).__name__), \
                     patch.dict(multi.os.environ, {"TEST_PLAYLIST": "https://example.test/list"}), \
                     patch.object(multi, "private_playlist_text", side_effect=[failure]), \
                     patch.object(multi.Gateway, "run", lambda gw: gw.stop.wait(2)), \
                     patch.object(multi, "ThreadingHTTPServer"), patch("builtins.print"):
                    self.assertEqual(multi.main(args), 0)

    def test_replacement_waits_for_inflight_upstream_request(self):
        channels = multi.channels_from_entries([
            ("A", "", "http://example.test/a"), ("B", "", "http://example.test/b")])
        manager = multi.MultiGateway(channels, Path("unused"), max_streams=1)
        reading, release, replacement = threading.Event(), threading.Event(), threading.Event()

        def fetch(url, timeout):
            if url == channels[0].url:
                reading.set()
                release.wait(5)
            else:
                replacement.set()
            raise OSError("offline")

        with patch("iptv_gateway.media_manifest", side_effect=fetch):
            first = manager.start(channels[0].id)
            second = None
            try:
                self.assertTrue(reading.wait(2))
                first.accessed -= 25
                second = manager.start(channels[1].id)
                self.assertIsNotNone(second)
                self.assertFalse(replacement.wait(0.2), "upstream requests overlapped")
                release.set()
                self.assertTrue(replacement.wait(2))
            finally:
                manager.close()
                release.set()
                first.worker.join(2)
                if second is not None:
                    second.worker.join(2)


if __name__ == "__main__":
    unittest.main()
