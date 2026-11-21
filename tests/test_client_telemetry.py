"""Regression coverage for local TV/phone request telemetry."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import iptv_gateway as g


class ClientTelemetryTests(unittest.TestCase):
    def test_counts_tv_requests_and_wrong_token_without_leaking_paths(self):
        gw = g.Gateway("https://provider.example/SECRET_ORIGIN_TOKEN", delay=30)
        for seq in range(100, 118):
            gw.cache[seq] = (10.0, b"0123456789")
            gw.total_bytes += 10
        gw.latest = 117
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     g.make_handler(gw, "private-gateway-token", "Test"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/private-gateway-token/status") as response:
                before = json.load(response)
            self.assertIsNone(before["last_published_sequence"])
            self.assertEqual(before["client_playlist_200"], 0)
            self.assertEqual(before["client_manifest_200"], 0)
            self.assertEqual(before["client_auth_404"], 0)

            with urllib.request.urlopen(root + "/private-gateway-token/channels.m3u") as response:
                self.assertIn(b"index.m3u8", response.read())
            with urllib.request.urlopen(root + "/private-gateway-token/index.m3u8") as response:
                self.assertIn(b"#EXTM3U", response.read())
            with urllib.request.urlopen(root + "/private-gateway-token/segment/116.ts") as response:
                self.assertEqual(response.read(), b"0123456789")
            range_req = urllib.request.Request(
                root + "/private-gateway-token/segment/116.ts",
                headers={"Range": "bytes=2-3"})
            with urllib.request.urlopen(range_req) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"23")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(root + "/stale-old-token/index.m3u8")
            self.assertEqual(missing.exception.code, 404)
            with urllib.request.urlopen(root + "/private-gateway-token/status") as response:
                status = json.load(response)
            self.assertEqual(status["client_playlist_200"], 1)
            self.assertEqual(status["client_manifest_200"], 1)
            self.assertEqual(status["client_segment_200"], 1)
            self.assertEqual(status["client_segment_206"], 1)
            self.assertEqual(status["client_auth_404"], 1)
            self.assertEqual(status["last_published_sequence"], 114)
            self.assertIsInstance(status["seconds_since_client_manifest"], (float, int))
            self.assertIsInstance(status["seconds_since_client_segment"], (float, int))
            self.assertNotIn("SECRET_ORIGIN_TOKEN", json.dumps(status))
            self.assertNotIn("private-gateway-token", json.dumps(status))
            self.assertNotIn("stale-old-token", json.dumps(status))
        finally:
            server.shutdown()
            server.server_close()

    def test_reading_status_does_not_fake_tv_activity(self):
        gw = g.Gateway("https://provider.example/hidden", delay=30)
        for seq in range(10, 24):
            gw.cache[seq] = (10.0, b"x")
        gw.latest = 23
        for _ in range(5):
            status = json.loads(gw.status())
            self.assertIsNone(status["last_published_sequence"])
        self.assertEqual(status["client_manifest_200"], 0)
        self.assertIsNone(status["seconds_since_client_manifest"])

    def test_503_and_missing_segment_still_counted(self):
        gw = g.Gateway("http://provider.test", delay=90)
        for seq in range(1, 3):
            gw.cache[seq] = (10, b"x")
        gw.latest = 2
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     g.make_handler(gw, "example-secret-token", "Test"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}/example-secret-token"
        try:
            for suffix, code in (("/index.m3u8", 503), ("/segment/123.ts", 404)):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(base + suffix)
                self.assertEqual(failure.exception.code, code)
            status = json.loads(gw.status())
            self.assertEqual(status["client_manifest_503"], 1)
            self.assertEqual(status["client_segment_404"], 1)
            self.assertEqual(status["client_manifest_200"], 0)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
