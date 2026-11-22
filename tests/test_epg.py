import gzip
import unittest
import xml.etree.ElementTree as ET

from iptv_epg import merge, normalize
from iptv_multi import channels_from_text, MultiGateway


PLAYLIST = """#EXTM3U
#EXTINF:-1 group-title="News",Россия 24 HD
https://example.test/stream1
#EXTINF:-1 group-title="News",Россия 24 SD
https://example.test/stream2
#EXTINF:-1 group-title="Other",ТВ 3
https://example.test/stream3
"""

GUIDE = b"""<?xml version="1.0" encoding="utf-8"?>
<tv><channel id="russia24"><display-name>Россия 24</display-name></channel>
<programme channel="russia24" start="20261122080000 +0000" stop="20261122090000 +0000">
<title>Новости</title></programme></tv>"""


class EpGTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize("ТЕСТ Ё HD"), normalize("тест е SD"))

    def test_merge_matches_quality_variants_and_real_programme_only(self):
        xml, report = merge(PLAYLIST, [GUIDE])
        root = ET.fromstring(xml)
        ids = [c.id for c in channels_from_text(PLAYLIST)]
        self.assertEqual(report["channels_with_epg"], 2)
        self.assertEqual(report["programmes"], 2)
        self.assertEqual([p.get("channel") for p in root.findall("programme")], ids[:2])
        self.assertNotIn(ids[2], [e.get("id") for e in root.findall("channel")])

    def test_first_source_wins_conflicting_programme(self):
        competing = GUIDE.replace(b"Новости", b"Other") if False else GUIDE.replace(
            "Новости".encode(), "Другая передача".encode())
        xml, report = merge(PLAYLIST, [GUIDE, competing])
        self.assertEqual(report["programmes"], 2)
        self.assertEqual(ET.fromstring(xml).findtext("programme/title"), "Новости")

    def test_m3u_guide_url_and_matching_ids(self):
        channels = channels_from_text(PLAYLIST)
        manager = MultiGateway(channels, __import__("pathlib").Path("not-used"))
        text = manager.playlist("safe-token", "localhost", public_base_url="https://tv.example.test",
                                epg_enabled=True).decode()
        self.assertIn('x-tvg-url="https://tv.example.test/safe-token/epg.xml.gz"', text)
        for channel in channels:
            self.assertIn(f'tvg-id="{channel.id}"', text)
        self.assertNotIn("example.test/stream", text)


if __name__ == "__main__":
    unittest.main()
