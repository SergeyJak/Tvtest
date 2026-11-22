"""Regression tests for cached, streamed XMLTV responses."""
import gzip
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from iptv_multi import MultiGateway, channels_from_entries, make_multi_handler


class CachedEpgTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.epg = Path(self.tmp.name) / "epg.xml.gz"
        self.payload = gzip.compress(b"<tv><channel id='one'/></tv>")
        self.epg.write_bytes(self.payload)
        self.manager = MultiGateway(
            channels_from_entries([("One", "Demo", "http://source.invalid/private")]),
            Path(self.tmp.name) / "private.m3u")
        self.token = "test-token-1234567890"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_multi_handler(self.manager, self.token,
                               public_base_url="https://tv.example.test",
                               epg_file=self.epg))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/{self.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.manager.close()
        self.tmp.cleanup()

    def request(self, headers=None, method="GET", path="epg.xml.gz"):
        return urllib.request.urlopen(
            urllib.request.Request(self.base + "/" + path, headers=headers or {}, method=method),
            timeout=3)

    def test_gzip_content_and_http_validators(self):
        with self.request() as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.payload)
            self.assertEqual(response.headers.get("Content-Type"), "application/gzip")
            self.assertEqual(int(response.headers.get("Content-Length")), len(self.payload))
            self.assertIn("private", response.headers.get("Cache-Control"))
            self.assertIn("max-age=300", response.headers.get("Cache-Control"))
            self.assertTrue(response.headers.get("ETag").startswith('W/"'))
            self.assertTrue(response.headers.get("Last-Modified"))
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

    def test_head_sends_metadata_without_body(self):
        with self.request(method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertEqual(int(response.headers["Content-Length"]), len(self.payload))

    def test_matching_etag_returns_304_with_no_payload(self):
        with self.request() as response:
            etag = response.headers["ETag"]
        for header in ({"If-None-Match": etag},
                       {"If-None-Match": etag.removeprefix("W/")},
                       {"If-None-Match": '"something-else", ' + etag}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(headers=header)
            self.assertEqual(error.exception.code, 304)
            self.assertEqual(error.exception.read(), b"")
            self.assertEqual(error.exception.headers["ETag"], etag)

    def test_last_modified_and_precedence(self):
        with self.request() as response:
            modified = response.headers["Last-Modified"]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(headers={"If-Modified-Since": modified})
        self.assertEqual(error.exception.code, 304)
        with self.request(headers={"If-None-Match": '"not-the-etag"',
                                   "If-Modified-Since": modified}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.payload)
        with self.request(headers={"If-Modified-Since": "not-a-date"}) as response:
            self.assertEqual(response.status, 200)

    def test_replaced_epg_changes_version_and_bytes(self):
        with self.request() as response:
            prior = response.headers["ETag"]
            old_mtime = self.epg.stat().st_mtime
        new = gzip.compress(b"<tv><programme channel='one'/></tv>")
        self.epg.write_bytes(new)
        os.utime(self.epg, (old_mtime + 2, old_mtime + 2))
        with self.request(headers={"If-None-Match": prior}) as response:
            self.assertEqual(response.status, 200)
            self.assertNotEqual(response.headers["ETag"], prior)
            self.assertEqual(response.read(), new)

    def test_missing_or_invalid_file_fails_closed(self):
        self.epg.write_bytes(b"not gzip")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request()
        self.assertEqual(error.exception.code, 503)
        self.epg.unlink()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request()
        self.assertEqual(error.exception.code, 503)

    def test_m3u_headers_expose_both_epg_discovery_variants(self):
        with self.request(path="channels.m3u") as response:
            playlist = response.read().decode()
        self.assertIn('x-tvg-url="https://tv.example.test/' + self.token + '/epg.xml.gz"', playlist)
        self.assertIn('url-tvg="https://tv.example.test/' + self.token + '/epg.xml.gz"', playlist)
        self.assertEqual(playlist.count('tvg-id="'), 1)
        self.assertNotIn("source.invalid", playlist)

    def test_wrong_token_cannot_read_epg(self):
        url = self.base.replace(self.token, "incorrect-token") + "/epg.xml.gz"
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url, timeout=3)
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
