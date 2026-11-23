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

    def test_m3u_only_refresh_after_region_change_skips_epg_generation(self):
        from iptv_profile_jobs import run_update
        with patch("iptv_profiles.download_playlist", return_value=PLAYLIST):
            row, _ = self.store.update(2, "Резерв", "https://example.org/playlist")
        before = self.store.get(2)["revision"]
        rotated = PLAYLIST.replace("PRIVATE_CHANNEL", "DIFFERENT_CDN")
        with patch("iptv_profile_jobs.download_playlist", return_value=rotated), \
             patch("iptv_epg_stream.build_epg", side_effect=AssertionError("EPG must not run")):
            result = run_update(self.store, [2], refresh_m3u=True, m3u_only=True)
        self.assertEqual(result[2], "refreshed_m3u")
        self.assertEqual(self.store.get(2)["revision"], before)
        self.assertIn("DIFFERENT_CDN", self.store.snapshot_path(2).read_text())
        # Refreshing provider URLs must never disclose the private source key.
        self.assertNotIn("https://example.org/playlist", str(result))

    def test_m3u_only_keeps_previous_epg_on_channel_list_change_and_failure(self):
        from iptv_profile_jobs import run_update
        with patch("iptv_profiles.download_playlist", return_value=PLAYLIST):
            self.store.update(2, "Резерв", "https://example.org/playlist")
        old_guide = b"previous-successful-guide-content"
        self.store.epg_path(2).write_bytes(old_guide)
        previous_rev = self.store.get(2)["revision"]
        new_playlist = PLAYLIST + (
            '#EXTINF:-1 group-title="News",New Channel\n'
            'https://provider.invalid/new-channel\n')
        with patch("iptv_profile_jobs.download_playlist", return_value=new_playlist):
            result = run_update(self.store, [2], refresh_m3u=True, m3u_only=True)
        self.assertEqual(result[2], "refreshed_m3u")
        self.assertEqual(self.store.get(2)["revision"], previous_rev + 1)
        self.assertEqual(self.store.epg_path(2).read_bytes(), old_guide)
        # Even a failed build after changing channel IDs cannot destroy the
        # last good guide, which should be atomically replaced only on success.
        with patch.object(self.store, "guide_source", return_value="https://example.org/guide"), \
             patch("iptv_profile_jobs.validate_source_url", side_effect=lambda url: url), \
             patch("iptv_epg_stream.build_epg", side_effect=OSError("source failure")):
            failure = run_update(self.store, [2])
        self.assertEqual(failure[2], "failed")
        self.assertEqual(self.store.epg_path(2).read_bytes(), old_guide)
        with patch.object(self.store, "guide_source", return_value="https://example.org/guide"), \
             patch("iptv_profile_jobs.validate_source_url", side_effect=lambda url: url), \
             patch("iptv_epg_stream.build_epg",
                   side_effect=lambda playlist, urls, destination, opts:
                   Path(destination).write_bytes(b"new-guide")):
            success = run_update(self.store, [2])
        self.assertEqual(success[2], "updated")
        self.assertEqual(self.store.epg_path(2).read_bytes(), b"new-guide")

    def test_vf_virtual_ids_survive_backup_becoming_available_during_playback(self):
        from collections import OrderedDict
        playlist = ('#EXTM3U\n#EXTINF:-1,VF Теория большого взрыва\n'
                    'https://cdn1.example/private-video\n')
        with patch("iptv_profiles.download_playlist", return_value=playlist):
            self.store.update(1, "Основной", "https://provider.example/primary.m3u")
        token = self.store.get(1)["token"]
        manager = self.registry.resolve(token)[0]
        cid = next(iter(manager.channels))
        mux, candidates = self.registry.failover_for(token, cid)
        self.assertEqual(len(candidates), 1)
        primary = candidates[0][1]
        with primary.gateway.lock:
            primary.gateway.cache = OrderedDict(
                (i, (6.0, f"PRIMARY-{i}".encode())) for i in range(100, 106))
            primary.gateway.latest = 105
            primary.gateway.target = 6.0
            primary.gateway.last_progress = time.time()
            primary.gateway.last_segment_download = time.time()
            primary.gateway._fast_start_at = time.monotonic() - 1
        with self.request(token, f"channel/{cid}/index.m3u8") as response:
            baseline = response.read().decode()
        self.assertIn("MEDIA-SEQUENCE:1", baseline)
        with self.request(token, f"channel/{cid}/segment/1.ts") as response:
            self.assertEqual(response.read(), b"PRIMARY-100")
        # The other account is configured while the TV continues watching.
        with patch("iptv_profiles.download_playlist", return_value=playlist):
            self.store.update(2, "Резерв", "https://provider.example/backup.m3u")
        self.registry.resolve(token)
        updated_mux, expanded = self.registry.failover_for(token, cid)
        self.assertIs(updated_mux, mux)
        self.assertEqual(len(expanded), 2)
        with self.request(token, f"channel/{cid}/index.m3u8") as response:
            later = response.read().decode()
        self.assertIn(f"/{token}/channel/{cid}/segment/1.ts", later)
        with self.request(token, f"channel/{cid}/segment/1.ts") as response:
            self.assertEqual(response.read(), b"PRIMARY-100")

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


    def test_cdn_diagnostics_explain_empty_monitor_before_pilot_activation(self):
        owner_token = self.store.get(1)["token"]
        diagnostics = self.registry.pilot_diagnostics(owner_token)
        self.assertIsNotNone(diagnostics)
        self.assertTrue(diagnostics["enabled"])
        self.assertFalse(diagnostics["primary_channel_found"])
        self.assertFalse(diagnostics["pilot_active"])
        self.assertIsNone(self.registry.pilot_diagnostics("invalid-token"))

        # A similarly named, but non-pilot channel must be visible as a
        # diagnostic hint rather than silently opening extra CDN streams.
        near_match = ("#EXTM3U\n"
                      "#EXTINF:-1,VF Теория большого взрыва HD\n"
                      "https://provider.example/PRIVATE_KEY\n")
        with patch("iptv_profiles.download_playlist", return_value=near_match):
            self.store.update(1, "Основной", "https://provider.example/catalog")
        self.registry.resolve(owner_token)
        diagnostics = self.registry.pilot_diagnostics(owner_token)
        self.assertFalse(diagnostics["primary_channel_found"])
        self.assertEqual(diagnostics["possible_names"], ["VF Теория большого взрыва HD"])
        with self.request(owner_token, "status") as response:
            status = json.load(response)
        self.assertIn("multi_cdn_diagnostics", status)
        self.assertNotIn("PRIVATE_KEY", json.dumps(status))
        self.assertNotIn("provider.example", json.dumps(status))
        self.assertEqual(status["multi_cdn_diagnostics"]["possible_names"],
                         ["VF Теория большого взрыва HD"])

    def test_cdn_diagnostics_show_configured_pilot_without_active_viewer(self):
        playlist = ("#EXTM3U\n"
                    "#EXTINF:-1,VF Теория большого взрыва\n"
                    "https://provider.example/SECRET_CDN\n")
        with patch("iptv_profiles.download_playlist", return_value=playlist):
            self.store.update(1, "Основной", "https://provider.example/catalog")
        owner_token = self.store.get(1)["token"]
        self.registry.resolve(owner_token)
        diagnostic = self.registry.pilot_diagnostics(owner_token)
        self.assertTrue(diagnostic["primary_channel_found"])
        self.assertFalse(diagnostic["pilot_active"])
        self.assertFalse(diagnostic["cabins"][0]["playing"])
        first_manager = self.registry.resolve(owner_token)[0]
        channel = next(iter(first_manager.channels))
        self.registry.failover_for(owner_token, channel)
        diagnostic = self.registry.pilot_diagnostics(owner_token)
        self.assertTrue(diagnostic["pilot_active"])
        self.assertTrue(diagnostic["cabins"][0]["playing"])
        self.assertNotIn("SECRET_CDN", json.dumps(diagnostic))

    def test_primary_vf_channel_uses_three_cdns_without_changing_tv_links(self):
        """Real HTTP path keeps one TV link and never exposes CDN URLs."""
        from collections import OrderedDict
        for slot in (1, 2, 3):
            playlist = ("#EXTM3U\n"
                        "#EXTINF:-1 group-title=\"VF\",VF Теория большого взрыва\n"
                        f"https://cdn{slot}.example/PRIVATE_{slot}/index.m3u8\n")
            with patch("iptv_profiles.download_playlist", return_value=playlist):
                row, changed = self.store.update(
                    slot, f"CDN-{slot}", f"https://provider{slot}.example/M3U_KEY_{slot}")
            self.assertTrue(changed)
        owner_token = self.store.get(1)["token"]
        backup_token = self.store.get(2)["token"]
        manager = self.registry.resolve(owner_token)[0]
        cid = next(iter(manager.channels))
        self.assertIsNone(self.registry.failover_for(backup_token, cid))
        pilot = self.registry.failover_for(owner_token, cid)
        self.assertIsNotNone(pilot)
        mux, candidates = pilot
        self.assertEqual([slot for slot, _ in candidates], [1, 2, 3])
        for slot, stream in candidates:
            gateway = stream.gateway
            now = time.time()
            with gateway.lock:
                gateway.cache = OrderedDict(
                    (i, (6.0, f"VIDEO_CDN_{slot}_{i}".encode()))
                    for i in range(100, 106))
                gateway.latest = 105
                gateway.target = 6.0
                gateway.last_progress = now
                gateway.last_segment_download = now
                gateway._fast_start_at = time.monotonic() - 2
        with self.request(owner_token, f"channel/{cid}/index.m3u8") as response:
            first_manifest = response.read().decode()
        self.assertIn(f"/{owner_token}/channel/{cid}/segment/1.ts", first_manifest)
        self.assertNotIn("PRIVATE_1", first_manifest)
        self.assertNotIn("M3U_KEY_1", first_manifest)
        with self.request(owner_token, f"channel/{cid}/segment/1.ts") as response:
            self.assertEqual(response.read(), b"VIDEO_CDN_1_100")
        self.assertEqual(mux.source_slot, 1)
        # Trigger a source stall and verify a stable virtual media sequence.
        with candidates[0][1].gateway.lock:
            candidates[0][1].gateway.last_progress -= 90
            candidates[0][1].gateway.last_segment_download -= 90
        mux.last_switch = time.monotonic() - 25
        with self.request(owner_token, f"channel/{cid}/index.m3u8") as response:
            next_manifest = response.read().decode()
        self.assertIn("#EXT-X-DISCONTINUITY", next_manifest)
        self.assertIn(f"/{owner_token}/channel/{cid}/segment/4.ts", next_manifest)
        with self.request(owner_token, f"channel/{cid}/segment/4.ts") as response:
            self.assertEqual(response.read(), b"VIDEO_CDN_2_100")
        self.assertEqual(mux.source_slot, 2)
        with self.request(owner_token, "status") as response:
            live = json.load(response)
        self.assertEqual(live["multi_cdn"][0]["selected_cabinet"], 2)
        self.assertEqual(live["multi_cdn"][0]["switches"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(live))
        self.assertNotIn("M3U_KEY_", json.dumps(live))
        # Operator can disable the pilot without deleting M3U or changing keys.
        (self.store.directory / "multicdn.disabled").touch()
        self.assertIsNone(self.registry.failover_for(owner_token, cid))
        self.assertIsNone(self.registry.existing_failover(owner_token, cid))


if __name__ == "__main__":
    unittest.main()
