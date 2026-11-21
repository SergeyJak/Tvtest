import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iptv_healthcheck as h
import iptv_probe as p


class TestPlaylist(unittest.TestCase):
    def test_parse_m3u(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "private.m3u8"
            file.write_text("#EXTM3U\n#EXTINF:0,News HD\n#EXTGRP:News\nhttps://example.com/secret/live.m3u8\n", encoding="utf-8")
            self.assertEqual(h.entries_from_m3u(file), [
                ("News HD", "News", "https://example.com/secret/live.m3u8")
            ])

    def test_choose_by_channel(self):
        channels = [("BBC HD", "News", "https://example.com/a.m3u8"),
                    ("Test HD", "News", "https://example.com/b.m3u8")]
        self.assertEqual(h.choose(channels, ["BBC"], 8), channels[:1])

    def test_hls_segment_url(self):
        media = "#EXTM3U\n#EXTINF:5.0,\nchunk1.ts\n"
        self.assertEqual(h.hls_next_url(media, "https://example.com/live/index.m3u8"),
                         "https://example.com/live/chunk1.ts")

    def test_error_details_do_not_leak_stream_key(self):
        entry = ("Test", "News", "https://example.com/SECRET/live.m3u8")
        with patch("iptv_healthcheck.get_text", side_effect=ValueError("SECRET")):
            self.assertEqual(h.run(entry, 1, 1024)["status"], "ERROR: ValueError")

    def test_excel_formula_names_are_escaped(self):
        self.assertEqual(p.safe_cell("=IMPORTXML('x')"), "'=IMPORTXML('x')")

    def test_dry_run_never_fetches(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private.m3u8"
            path.write_text("#EXTM3U\n#EXTINF:0,Test\nhttps://example.com/key/live.m3u8\n")
            with patch("iptv_healthcheck.get_text", side_effect=AssertionError("unexpected network")):
                self.assertEqual(p.main(["--playlist", str(path), "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
