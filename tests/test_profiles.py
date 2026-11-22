"""Three independently configured IPTV cabinets without upstream web calls."""
import gzip
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from iptv_multi import MultiGateway, channels_from_text, make_multi_handler
from iptv_profiles import ProfileStore, ProfileError
from iptv_profile_gateway import ProfileRegistry
from iptv_profile_jobs import generate_for_slot


PLAYLIST = """#EXTM3U
#EXTINF:-1 group-title="News",Channel A
https://provider.invalid/PRIVATE_CHANNEL_A
#EXTINF:-1 group-title="Sports",Channel B
https://provider.invalid/PRIVATE_CHANNEL_B
"""
OTHER = """#EXTM3U
#EXTINF:-1 group-title="News",Channel C
https://provider.invalid/OTHER_SECRET
"""


class ThreeCabinetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.legacy = root / "forgpt.m3u8"
        self.legacy.write_text(PLAYLIST)
        self.legacy_token = root / "legacy-token"
        self.legacy_token.write_text("stable-original-token-abcdef", encoding="ascii")
        self.legacy_epg = root / "epg.xml.gz"
        self.legacy_epg.write_bytes(gzip.compress(b"<tv></tv>"))
        self.store = ProfileStore(root / ".iptv_profiles", self.legacy,
                                  self.legacy_epg, self.legacy_token)
        self.mock_run = patch("iptv_multi.Gateway.run", lambda g: g.stop.wait(3))
        self.mock_run.start()
        self.base_manager = MultiGateway(channels_from_text(PLAYLIST), self.legacy,
                                         max_streams=2, startup_wait=0)
        self.registry = ProfileRegistry(
            self.store, self.base_manager, "stable-original-token-abcdef",
            self.legacy_epg,
            dict(max_streams=2, startup_wait=0))
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_multi_handler(self.base_manager, "stable-original-token-abcdef",
                               "https://tv.example.test", self.legacy_epg,
                               profile_registry=self.registry))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.registry.close()
        self.base_manager.close()
        self.mock_run.stop()
        self.temp.cleanup()

    def request(self, token: str, path: str):
        return urllib.request.urlopen(f"{self.base}/{token}/{path}", timeout=3)

    def test_primary_original_token_preserved_and_other_slots_empty(self):
        profiles = self.store.public_profiles("https://tv.example.test")
        self.assertEqual(len(profiles), 3)
        self.assertEqual(profiles[0]["m3u"],
                         "https://tv.example.test/stable-original-token-abcdef/channels.m3u")
        self.assertIsNone(profiles[1]["m3u"])
        self.assertIsNone(profiles[2]["m3u"])
        with self.request("stable-original-token-abcdef", "channels.m3u") as response:
            self.assertEqual(response.read().decode().count("#EXTINF:"), 2)
        tokens = [row["token"] for row in self.store.all()]
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertNotIn("PRIVATE_CHANNEL_A", json.dumps(profiles))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)

    def test_set_two_profiles_from_separate_m3u_urls(self):
        with patch("iptv_profiles.download_playlist", side_effect=[OTHER, PLAYLIST]):
            second, changed2 = self.store.update(2, "Родители",
                "https://example.test/private?token=SECRET_A")
            third, changed3 = self.store.update(3, "Кристина",
                "https://example.test/private?token=SECRET_B")
        self.assertTrue(changed2)
        self.assertTrue(changed3)
        self.assertNotEqual(second["token"], third["token"])
        self.assertNotEqual(second["token"], self.store.get(1)["token"])
        with self.request(second["token"], "channels.m3u") as response:
            body2 = response.read().decode()
        with self.request(third["token"], "channels.m3u") as response:
            body3 = response.read().decode()
        self.assertEqual(body2.count("#EXTINF:"), 1)
        self.assertEqual(body3.count("#EXTINF:"), 2)
        self.assertIn("Channel C", body2)
        self.assertIn("Channel A", body3)
        self.assertNotIn("SECRET_A", body2)
        self.assertNotIn("SECRET_B", body3)
        self.assertNotIn("OTHER_SECRET", body2)
        self.assertNotIn("PRIVATE_CHANNEL_A", body3)
        self.assertEqual(self.registry.resolve(second["token"])[0].max_streams, 1)
        public = self.store.public_profiles("https://tv.example.test")
        self.assertEqual(public[1]["name"], "Родители")
        self.assertEqual(public[2]["name"], "Кристина")
        self.assertEqual(public[1]["source_host"], "example.test")
        self.assertNotIn("SECRET_A", json.dumps(public))
        self.assertNotIn("SECRET_B", json.dumps(public))
        self.assertFalse(any("source_url" in row for row in public))

    def test_rewrite_one_profile_does_not_change_other_tokens(self):
        with patch("iptv_profiles.download_playlist", return_value=OTHER):
            p2, _ = self.store.update(2, "Кабинет А", "https://example.org/m3u")
        original_token = self.store.get(1)["token"]
        with patch("iptv_profiles.download_playlist", return_value=PLAYLIST):
            new_p2, _ = self.store.update(2, "Папа", "https://example.org/new-m3u")
        self.assertEqual(p2["token"], new_p2["token"])
        self.assertEqual(self.store.get(1)["token"], original_token)
        with self.request(p2["token"], "channels.m3u") as response:
            self.assertIn("Channel B", response.read().decode())
        with self.request(original_token, "channels.m3u") as response:
            self.assertIn("Channel A", response.read().decode())

    def test_scheduled_url_rotation_updates_new_workers_and_keeps_active_viewers(self):
        with patch("iptv_profiles.download_playlist", return_value=PLAYLIST):
            row, _ = self.store.update(1, "TV", "https://example.test/catalog")
        manager = self.registry.resolve(row["token"])[0]
        first, second = manager.order
        active = manager.start(first)
        rotated = PLAYLIST.replace("PRIVATE_CHANNEL", "ROTATED_CHANNEL")
        with patch("iptv_profile_jobs.download_playlist", return_value=rotated):
            generate_for_slot(self.store, 1, refresh_m3u=True)
        updated = self.registry.resolve(row["token"])[0]
        self.assertIs(updated.start(first), active)
        self.assertFalse(active.gateway.stop.is_set())
        self.assertEqual(updated.start(second).gateway._url, channels_from_text(rotated)[1].url)

    def test_rename_is_instant_and_does_not_restart_workers(self):
        with patch("iptv_profiles.download_playlist", return_value=OTHER):
            row, _ = self.store.update(2, "Old", "https://example.org/m3u")
        before = self.registry.resolve(row["token"])[0]
        self.store.update(2, "New")
        after = self.registry.resolve(row["token"])[0]
        self.assertIs(before, after)
        self.assertEqual(self.store.get(2)["name"], "New")

    def test_wrong_or_unconfigured_profile_token_rejected(self):
        token2 = self.store.get(2)["token"]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(token2, "channels.m3u")
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("wrong-token", "channels.m3u")
        self.assertEqual(error.exception.code, 404)

    def test_private_source_not_changed_if_new_playlist_invalid(self):
        with patch("iptv_profiles.download_playlist", return_value="not a playlist"):
            with self.assertRaises(ProfileError):
                self.store.update(2, "Untrusted", "https://example.org/private")
        self.assertEqual(self.store.get(2)["source_url"], "")
        self.assertFalse(self.store.snapshot_path(2).exists())

    def test_epg_is_generated_for_the_correct_cabinet_only(self):
        xml = (b'<tv><channel id="guide-c">'
               b'<display-name>Channel C</display-name></channel>'
               b'<programme channel="guide-c" start="20261122110000 +0000" '
               b'stop="20261122120000 +0000">'
               b'<title>Sample programme</title></programme></tv>')
        epg_source = Path(self.temp.name) / "provider-epg.xml.gz"
        epg_source.write_bytes(gzip.compress(xml))
        with patch("iptv_profiles.download_playlist", return_value=OTHER), \
             patch("iptv_profiles.validate_source_url", side_effect=lambda value: value):
            row, _ = self.store.update(
                2, "EPG Source", "https://provider.example/m3u",
                str(epg_source))
        with patch("iptv_profile_jobs.validate_source_url", side_effect=lambda value: value):
            status = generate_for_slot(self.store, 2)
        self.assertEqual(status, "updated")
        with gzip.open(self.store.epg_path(2), "rb") as source:
            result = source.read().decode()
        self.assertIn("Sample programme", result)
        self.assertIn(channels_from_text(OTHER)[0].id, result)
        self.assertNotIn(channels_from_text(PLAYLIST)[0].id, result)
        self.assertTrue(self.store.legacy_epg.is_file())
        self.assertEqual(self.registry.resolve(row["token"])[2], self.store.epg_path(2))

    def test_first_profile_switches_source_without_changing_url(self):
        before = self.store.public_profiles("https://tv.example.test")[0]["m3u"]
        with patch("iptv_profiles.download_playlist", return_value=OTHER):
            row, changed = self.store.update(1, "Мой ТВ",
                "https://example.test/new_playlist")
        self.assertTrue(changed)
        self.assertEqual(self.registry.resolve(row["token"])[0].max_streams, 2)
        with self.request(row["token"], "channels.m3u") as response:
            body = response.read().decode()
        self.assertIn("Channel C", body)
        self.assertNotIn("Channel A", body)
        self.assertEqual(self.store.public_profiles("https://tv.example.test")[0]["m3u"], before)
        self.assertEqual(self.legacy.read_text(), PLAYLIST)
        self.assertEqual(self.legacy_token.read_text(), "stable-original-token-abcdef")


if __name__ == "__main__":
    unittest.main()
