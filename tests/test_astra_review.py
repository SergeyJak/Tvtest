"""Offline regression tests for issues reported by the Astra review.

Local HTTP fixtures only; no private playlist URL or network dependency.
"""
import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import iptv_continuous as continuous
import iptv_gateway as gateway
import iptv_healthcheck as quick


class SourceHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/entry":
            self.send_response(302)
            self.send_header("Location", "/cdn/index.m3u8")
            self.end_headers()
            return
        if path == "/cdn/index.m3u8":
            content = (b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
                       b"#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:10,\n1.ts\n")
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if path == "/empty.m3u8":
            content = b"#EXTM3U\n#EXTINF:10,\n/empty.ts\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if path in ("/cdn/1.ts", "/short.ts", "/empty.ts"):
            if path == "/cdn/1.ts":
                data, claimed = b"\x47video", 6
            elif path == "/short.ts":
                data, claimed = b"\x47ab", 100
            else:
                data, claimed = b"", 0
            self.send_response(200)
            self.send_header("Content-Length", str(claimed))
            self.end_headers()
            if data:
                self.wfile.write(data)
                self.wfile.flush()
            self.close_connection = True
            return
        self.send_error(404)


class AstraReviewRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.root = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def test_http_redirect_resolves_media_relative_to_final_cdn_url(self):
        record = quick.run(("Test", "", self.root + "/entry"), 3, 1024)
        self.assertEqual(record["status"], "OK", record)
        media, _ttfb = continuous.fetch_media(self.root + "/entry", 3)
        self.assertEqual(media.segments[0].url, self.root + "/cdn/1.ts")
        # Older callers still receive the backward-compatible two-tuple.
        body, ms = quick.get_text(self.root + "/entry", 3)
        self.assertIn("#EXTM3U", body)
        self.assertIsInstance(ms, int)
        body, ms, final = quick.get_text(
            self.root + "/entry", 3, return_final_url=True)
        self.assertEqual(final, self.root + "/cdn/index.m3u8")

    def test_declared_length_shorter_than_body_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "truncated_segment"):
            gateway.fetch_segment(self.root + "/short.ts", 3, 2048)
        status, nbytes, _, _ = continuous.download_segment(
            self.root + "/short.ts", 3, 2048)
        self.assertEqual((status, nbytes), ("TRUNCATED_SEGMENT", 3))

    def test_empty_probe_segment_is_not_ok(self):
        with self.assertRaisesRegex(ValueError, "empty_segment"):
            quick.segment_probe(self.root + "/empty.ts", 3, 2048)
        record = quick.run(("Empty", "", self.root + "/empty.m3u8"), 3, 2048)
        self.assertEqual(record["status"], "EMPTY_SEGMENT")

    def test_gateway_retries_incomplete_chunked_read_without_worker_exit(self):
        manifest = ("#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
                    "#EXT-X-MEDIA-SEQUENCE:7\n#EXTINF:10,\n7.ts\n")
        gw = gateway.Gateway("http://upstream.example.test", delay=90)
        incomplete = http.client.IncompleteRead(
            b"\x47", 10)
        with patch.object(gateway, "media_manifest",
                          return_value=(manifest, "http://example.test/")), \
             patch.object(gateway, "fetch_segment",
                          side_effect=[incomplete, b"\x47complete"]):
            gw.cycle()
            self.assertIsNone(gw.segment(7))
            self.assertEqual(gw.last_error, "SEGMENT_INCOMPLETE_READ")
            gw.cycle()
            self.assertEqual(gw.segment(7), b"\x47complete")
            self.assertIsNone(gw.last_error)

    def test_monitor_logs_incomplete_read_and_continues(self):
        media = continuous.parse_media(
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n"
            "#EXT-X-MEDIA-SEQUENCE:10\n#EXTINF:5,\n10.ts\n",
            "http://example.test/stream.m3u8")
        clock = [100.]
        rows = []
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(continuous, "fetch_media", return_value=(media, 10)), \
             patch.object(continuous, "download_segment",
                          side_effect=http.client.IncompleteRead(b"a", 20)), \
             patch.object(continuous.time, "monotonic",
                          side_effect=lambda: clock[0]), \
             patch.object(continuous.time, "sleep", side_effect=sleep):
            continuous.observe(("Test", "", "http://example.test/entry"),
                               .03, .5, 2, 1024, rows.append)
        self.assertTrue(any(r["status"] == "SEGMENT_INCOMPLETE_READ"
                            for r in rows))

    def test_monitor_resumes_on_major_sequence_reset(self):
        old = continuous.parse_media(
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n"
            "#EXT-X-MEDIA-SEQUENCE:900\n#EXTINF:5,\n900.ts\n",
            "http://example.test/entry")
        fresh = continuous.parse_media(
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n"
            "#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXTINF:5,\n1.ts\n#EXTINF:5,\n2.ts\n#EXTINF:5,\n3.ts\n",
            "http://example.test/entry")
        clock, calls, rows = [100.], [0], []
        def fetch(*_):
            calls[0] += 1
            return (old if calls[0] == 1 else fresh), 10
        with patch.object(continuous, "fetch_media", side_effect=fetch), \
             patch.object(continuous, "download_segment",
                          return_value=("OK", 100, 10, 20)), \
             patch.object(continuous.time, "monotonic",
                          side_effect=lambda: clock[0]), \
             patch.object(continuous.time, "sleep",
                          side_effect=lambda n: clock.__setitem__(0, clock[0]+n)):
            continuous.observe(("Test", "", "http://example.test/entry"),
                               .04, .5, 2, 1024, rows.append)
        self.assertIn("SEQUENCE_RESET", [r["status"] for r in rows])
        self.assertEqual([r["sequence"] for r in rows if r["status"] == "OK"],
                         [900, 1, 2, 3])
        self.assertNotIn("STALE_MANIFEST", [r["status"] for r in rows])

    def test_first_publication_requires_real_delay_even_after_stall(self):
        gw = gateway.Gateway("http://example.test", delay=90)
        for seq in range(100, 103):
            gw.cache[seq] = (10, b"x")
        gw.latest = 102
        gw.last_advance_at = 10.0
        with patch.object(gateway.time, "monotonic", return_value=105.0):
            self.assertEqual(gw.published(), [])
            status = json.loads(gw.status())
        self.assertFalse(status["ready"])
        self.assertIsNone(status["last_published_sequence"])


if __name__ == "__main__":
    unittest.main()
