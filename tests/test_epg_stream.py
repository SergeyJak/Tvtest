import gzip
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from iptv_epg_stream import build_epg
from iptv_multi import channels_from_text


PLAYLIST = """#EXTM3U x-tvg-url="https://private.invalid/guide.xml.gz"
#EXTINF:-1,Россия 24 HD
https://example.invalid/stream1
#EXTINF:-1,Россия 24 SD
https://example.invalid/stream2
#EXTINF:-1,ТВ 3
https://example.invalid/stream3
"""


def guide(title="Новости", channel="russia24", count=1):
    programs = "".join(
        f'<programme channel="{channel}" start="20261122{i:06d} +0000" '
        f'stop="20261123{i:06d} +0000"><title>{title}</title></programme>'
        for i in range(count)
    )
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<tv><channel id="{channel}"><display-name>Россия 24</display-name></channel>'
            f'{programs}</tv>').encode()


class StreamingEpgTests(unittest.TestCase):
    def test_gzip_and_priority_with_multiple_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            (tmp / "first.gz").write_bytes(gzip.compress(guide()))
            (tmp / "second.xml").write_bytes(guide("Different"))
            output = tmp / "output.xml.gz"
            report = build_epg(PLAYLIST,
                               [str(tmp / "first.gz"), str(tmp / "second.xml")],
                               output)
            self.assertEqual(report["channels_total"], 3)
            self.assertEqual(report["channels_with_epg"], 2)
            self.assertEqual(report["programmes"], 2)
            root = ET.fromstring(gzip.decompress(output.read_bytes()))
            ids = [c.id for c in channels_from_text(PLAYLIST)]
            self.assertEqual([p.get("channel") for p in root.findall("programme")],
                             ids[:2])
            self.assertEqual({p.findtext("title") for p in root.findall("programme")},
                             {"Новости"})

    def test_invalid_guide_never_overwrites_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            output = tmp / "output.xml.gz"
            output.write_bytes(b"old-guide")
            (tmp / "invalid.xml").write_bytes(b"<tv><programme>")
            with self.assertRaises(ET.ParseError):
                build_epg(PLAYLIST, [str(tmp / "invalid.xml")], output)
            self.assertEqual(output.read_bytes(), b"old-guide")

    def test_unmatched_guide_never_overwrites_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            output = tmp / "output.xml.gz"
            output.write_bytes(b"old-guide")
            (tmp / "unmatched.xml").write_bytes(
                b'<tv><programme channel="unknown" start="20260101000000 +0000">'
                b'<title>Other</title></programme></tv>'
            )
            with self.assertRaises(ValueError):
                build_epg(PLAYLIST, [str(tmp / "unmatched.xml")], output)
            self.assertEqual(output.read_bytes(), b"old-guide")

    def test_explicit_alias_maps_unrelated_channel_id(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            (tmp / "other.xml").write_bytes(guide(channel="provider.some-id"))
            out = tmp / "epg.xml"
            report = build_epg(PLAYLIST, [str(tmp / "other.xml")], out,
                               {"ТВ 3": "provider.some-id"})
            self.assertEqual(report["channels_with_epg"], 1)
            self.assertEqual(report["programmes"], 1)
            root = ET.parse(out).getroot()
            self.assertEqual(root.find("programme").get("channel"),
                             channels_from_text(PLAYLIST)[2].id)

    def test_provider_metadata_matches_without_changing_public_ids(self):
        playlist = """#EXTM3U
#EXTINF:-1 tvg-id="provider-42" group-title="Sport",Local sports label
https://example.invalid/stream1
#EXTINF:-1 group-title="News" tvg-name="Original News",Local news label
https://example.invalid/stream2
"""
        original = playlist.replace(' tvg-id="provider-42"', '').replace(' tvg-name="Original News"', '')
        channels = channels_from_text(playlist)
        self.assertEqual([c.id for c in channels], [c.id for c in channels_from_text(original)])
        self.assertEqual(channels[0].tvg_id, "provider-42")
        self.assertEqual(channels[1].tvg_name, "Original News")
        xml = b"""<tv>
<channel id="provider-42"><display-name>Different sports label</display-name></channel>
<channel id="news"><display-name>Original News</display-name></channel>
<programme channel="provider-42" start="20261122080000 +0000"><title>Sport</title></programme>
<programme channel="news" start="20261122080000 +0000"><title>News</title></programme>
</tv>"""
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            source = tmp / "source.xml"
            source.write_bytes(xml)
            output = tmp / "output.xml"
            report = build_epg(playlist, [str(source)], output)
            self.assertEqual(report["channels_with_epg"], 2)
            self.assertEqual({p.get("channel"): p.findtext("title") for p in ET.parse(output).findall("programme")},
                             {channels[0].id: "Sport", channels[1].id: "News"})

    def test_many_listings_do_not_require_full_feed_in_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            source = tmp / "large.xml"
            source.write_bytes(guide(count=10000))
            output = tmp / "output.xml.gz"
            report = build_epg(PLAYLIST, [str(source)], output)
            self.assertEqual(report["scanned_programmes"], 10000)
            self.assertEqual(report["programmes"], 20000)


if __name__ == "__main__":
    unittest.main()
