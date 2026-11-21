"""Offline tests for recoverable HLS manifest 404s. All URLs are synthetic."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import iptv_gateway as g


def missing_404():
    return urllib.error.HTTPError("http://hidden.example/token/key", 404,
                                  "not found", None, None)


class PlaylistRefreshTests(unittest.TestCase):
    def test_exact_name_match_avoids_similar_channel(self):
        text = ("#EXTM3U\n#EXTINF:-1,ННТВ HD (Нижний Новгород)\n"
                "http://example.test/wrong.m3u8\n"
                "#EXTINF:-1,VF Теория большого взрыва\n"
                "http://example.test/right.m3u8\n")
        self.assertEqual(
            g.playlist_entry_from_text(text, "vf теория большого взрыва"),
            "http://example.test/right.m3u8")

    def test_no_duplicate_or_missing_channel_silent_selection(self):
        with self.assertRaises(ValueError):
            g.playlist_entry_from_text("#EXTM3U\n", "missing")
        duplicate = ("#EXTM3U\n#EXTINF:0,Channel\nhttp://a.example/a\n"
                     "#EXTINF:0,Channel\nhttp://b.example/b\n")
        with self.assertRaises(ValueError):
            g.playlist_entry_from_text(duplicate, "Channel")

    def test_local_file_reloaded_after_404(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private.m3u8"
            channel = "Channel"
            old = "http://example.test/old.m3u8"
            new = "http://example.test/new.m3u8"
            path.write_text(f"#EXTM3U\n#EXTINF:0,{channel}\n{new}\n", encoding="utf-8")
            def resolver():
                return g.playlist_entry_from_text(
                    g.private_playlist_text(path, None, 5), channel)
            gateway = g.Gateway(old, source_refresher=resolver)
            with patch.object(g, "media_manifest", side_effect=missing_404()):
                gateway.cycle()
            self.assertEqual(gateway._url, new)
            status = json.loads(gateway.status())
            self.assertEqual(status["consecutive_manifest_404"], 1)
            self.assertEqual(status["source_changes"], 1)
            self.assertEqual(status["source_refresh_attempts"], 1)
            self.assertEqual(status["last_error"], "MANIFEST_HTTP_404")
            self.assertNotIn("example.test", str(status))

    def test_same_source_not_reported_as_recovery(self):
        original = "http://example.test/old.m3u8"
        gateway = g.Gateway(original, source_refresher=lambda: original)
        with patch.object(g, "media_manifest", side_effect=missing_404()):
            gateway.cycle()
            gateway.cycle()
        self.assertEqual(gateway.source_changes, 0)
        self.assertEqual(gateway.source_refresh_attempts, 1)  # cooldown
        self.assertEqual(gateway.consecutive_manifest_404, 2)

    def test_success_clears_consecutive_404(self):
        original = "http://example.test/a.m3u8"
        gateway = g.Gateway(original, source_refresher=lambda: original)
        manifest = ("#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
                    "#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:10,\n1.ts\n")
        with patch.object(g, "media_manifest", side_effect=missing_404()):
            gateway.cycle()
        with patch.object(g, "media_manifest", return_value=(manifest, original)), \
             patch.object(g, "fetch_segment", return_value=b"data"):
            gateway.cycle()
        self.assertEqual(gateway.consecutive_manifest_404, 0)
        self.assertIsNone(gateway.last_error)

    def test_refresh_error_status_hides_secret(self):
        secret = "SENSITIVE_TOKEN"
        gateway = g.Gateway("http://example.test/old", source_refresher=lambda: (_ for _ in ()).throw(
            urllib.error.URLError(secret)))
        with patch.object(g, "media_manifest", side_effect=missing_404()):
            gateway.cycle()
        status = gateway.status().decode()
        self.assertIn('"source_refresh_errors": 1', status)
        self.assertNotIn(secret, status)

    def test_remote_playlist_fetch_is_size_bounded(self):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, n):
                return b"#EXTM3U\n#EXTINF:0,Channel\nhttp://example.test/live\n"
        with patch.object(g.urllib.request, 'urlopen', return_value=FakeResponse()):
            text = g.private_playlist_text(Path("not-used"), "https://example.test/private", 5)
        self.assertIn("Channel", text)

    def test_refresh_url_rejects_non_http_scheme(self):
        gateway = g.Gateway("http://old.test", source_refresher=lambda: "file:///private/secret")
        self.assertFalse(gateway.refresh_source())
        self.assertEqual(gateway.source_changes, 0)
        self.assertEqual(gateway.source_refresh_errors, 1)


if __name__ == "__main__":
    unittest.main()
