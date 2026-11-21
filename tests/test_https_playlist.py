"""HTTPS catalog URL regression tests for TLS reverse-proxy deployments."""

import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from iptv_multi import MultiGateway, channels_from_entries, make_multi_handler


class HttpsPlaylistTests(unittest.TestCase):
    def setUp(self):
        channels = channels_from_entries([
            ("Example Channel", "Demo", "http://upstream.invalid/private/stream"),
        ])
        self.manager = MultiGateway(channels, Path("unused.m3u"))

    def tearDown(self):
        self.manager.close()

    def test_default_catalog_retains_http_for_lan(self):
        body = self.manager.playlist("secret-token", "127.0.0.1:8787").decode()
        self.assertIn("http://127.0.0.1:8787/secret-token/channel/", body)

    def test_explicit_https_origin_overrides_request_host(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_multi_handler(
                self.manager, "secret-token", "https://tv.example.org"
            ),
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/secret-token/channels.m3u",
                timeout=3,
            ) as response:
                self.assertEqual(response.status, 200)
                body = response.read().decode()
            self.assertIn("https://tv.example.org/secret-token/channel/", body)
            self.assertNotIn("http://127.0.0.1:", body)
            self.assertNotIn("upstream.invalid", body)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
