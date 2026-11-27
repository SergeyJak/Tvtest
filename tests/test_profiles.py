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
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from iptv_multi import MultiGateway, channels_from_text, make_multi_handler
from iptv_profiles import ProfileStore, ProfileError
from iptv_profile_gateway import ProfileRegistry
from iptv_profile_jobs import generate_for_slot
from iptv_telemetry import Telemetry, query_incidents, export_incident_rows


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
        with self.request(token, f"channel/{cid}/segment/{mux.generation}.1.ts") as response:
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
        self.assertIn(f"/{token}/channel/{cid}/segment/{mux.generation}.1.ts", later)
        with self.request(token, f"channel/{cid}/segment/{mux.generation}.1.ts") as response:
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
        self.assertTrue(diagnostics["primary_channel_found"])
        self.assertFalse(diagnostics["pilot_active"])
        self.assertIsNone(self.registry.pilot_diagnostics("invalid-token"))

        # All channels are eligible; merely listing a channel never
        # starts workers or exposes private source URLs.
        near_match = ("#EXTM3U\n"
                      "#EXTINF:-1,VF Теория большого взрыва HD\n"
                      "https://provider.example/PRIVATE_KEY\n")
        with patch("iptv_profiles.download_playlist", return_value=near_match):
            self.store.update(1, "Основной", "https://provider.example/catalog")
        self.registry.resolve(owner_token)
        diagnostics = self.registry.pilot_diagnostics(owner_token)
        self.assertTrue(diagnostics["primary_channel_found"])
        self.assertFalse(diagnostics["pilot_active"])
        self.assertEqual(diagnostics["possible_names"], [])
        with self.request(owner_token, "status") as response:
            status = json.load(response)
        self.assertIn("multi_cdn_diagnostics", status)
        self.assertNotIn("PRIVATE_KEY", json.dumps(status))
        self.assertNotIn("provider.example", json.dumps(status))
        self.assertTrue(status["multi_cdn_diagnostics"]["primary_channel_found"])
        self.assertEqual(status["multi_cdn_diagnostics"]["possible_names"], [])

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

    def test_manifest_head_leaves_guest_place_for_first_get(self):
        tokens = []
        playlist = "#EXTM3U\n#EXTINF:-1,VF Теория большого взрыва\nhttps://cdn.example/vf\n"
        for slot in (1, 2, 3):
            with patch("iptv_profiles.download_playlist", return_value=playlist):
                row, _ = self.store.update(slot, f"TV {slot}", "https://provider.example/list")
            tokens.append(row["token"])
        third = self.registry.resolve(tokens[2])[0]
        cid = third.order[0]
        gateway = third.start(cid, standby=True).gateway
        for seq in range(100, 106):
            gateway.cache[seq] = (6.0, b"third")
        gateway.latest = 105
        gateway.last_progress = gateway.last_segment_download = time.time()
        gateway._fast_start_at = 0
        path = f"channel/{cid}/index.m3u8"
        probe = urllib.request.Request(f"{self.base}/{tokens[0]}/{path}", method="HEAD")
        with urllib.request.urlopen(probe) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        self.assertIsNone(self.registry.existing_failover(tokens[0], cid))
        self.assertFalse(self.registry.resolve(tokens[0])[0].active)
        with self.request(tokens[1], path) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.registry.existing_failover(tokens[1], cid).source_slot, 3)

    def test_duplicate_pilot_uses_requested_home_id(self):
        playlist = "#EXTM3U\n" + "".join(
            f"#EXTINF:-1,VF Теория большого взрыва\nhttps://cdn.example/{n}\n"
            for n in (1, 2))
        for slot in (2, 3):
            with patch("iptv_profiles.download_playlist", return_value=playlist):
                row, _ = self.store.update(slot, f"TV {slot}", "https://provider.example/list")
            manager = self.registry.resolve(row["token"])[0]
            requested = manager.order[1]
            mux, sources = self.registry.failover_for(row["token"], requested)
            source = dict(sources)[slot]
            self.assertEqual(source.channel.id, requested)
            self.assertIs(manager.start(requested), source)
            gw = source.gateway
            for seq in range(3):
                gw.cache[seq] = (6.0, b"second entry")
            gw.latest = 2
            gw.last_progress = gw.last_segment_download = time.time()
            gw._fast_start_at = 0
            with self.request(row["token"], f"channel/{requested}/index.m3u8") as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(mux.segment(1), b"second entry")

    def test_tv_status_only_reports_its_own_playing_flag(self):
        tokens = []
        for slot in (2, 3):
            with patch("iptv_profiles.download_playlist", return_value=OTHER.replace(
                    "Channel C", "VF Теория большого взрыва")):
                row, _ = self.store.update(slot, f"TV {slot}", "https://provider.example/list")
            tokens.append(row["token"])
        manager = self.registry.resolve(tokens[0])[0]
        manager.start(manager.order[0])
        for viewer, token in enumerate(tokens, start=2):
            with self.request(token, "status") as response:
                rows = json.load(response)["multi_cdn_diagnostics"]["cabins"]
            self.assertEqual({r["slot"]: r["playing"] for r in rows if "playing" in r},
                             {viewer: viewer == 2})

    def test_warmup_timeout_logs_missing_and_unconfigured_reserves(self):
        telemetry = Telemetry(Path(self.temp.name) / "diagnostics.db")
        self.addCleanup(telemetry.close)
        self.base_manager.telemetry = telemetry
        with patch("iptv_profiles.download_playlist", return_value=OTHER):
            self.store.update(2, "Second", "https://provider.invalid/PRIVATE")
        token = self.legacy_token.read_text()
        channel_id = self.base_manager.order[0]
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.request(token, f"channel/{channel_id}/index.m3u8")
        self.assertEqual(failed.exception.code, 503)
        telemetry.flush()
        rows = query_incidents(telemetry.path, kind="startup_timeout")["events"]
        context = rows[0]["diagnostics"]
        self.assertEqual(context["startup_wait_seconds"], 0)
        self.assertIsNone(context["selected_slot"])
        self.assertEqual(context["sources"]["2"]["state"], "NO_UNIQUE_CHANNEL_MATCH")
        self.assertEqual(context["sources"]["3"]["state"], "NOT_CONFIGURED")
        self.assertNotIn(token, json.dumps(rows))

    def test_busy_backup_exports_occupants_at_the_time_of_refusal(self):
        telemetry = Telemetry(Path(self.temp.name) / "diagnostics.db")
        self.addCleanup(telemetry.close)
        self.base_manager.telemetry = telemetry
        with patch("iptv_profiles.download_playlist", return_value=PLAYLIST):
            row, _ = self.store.update(2, "Second", "https://provider.invalid/PRIVATE")
        backup = self.registry.resolve(row["token"])[0]
        occupant = backup.start(backup.order[1], pinned=True)
        occupant.pending_requests = 2
        token = self.legacy_token.read_text()
        with self.assertRaises(urllib.error.HTTPError):
            self.request(token, f"channel/{self.base_manager.order[0]}/index.m3u8")
        occupant.pending_requests = 0
        telemetry.flush()
        [event] = export_incident_rows(telemetry.path, kind="startup_timeout")
        source = event["diagnostics"]["sources"]["2"]
        self.assertEqual(source["state"], "STREAM_LIMIT")
        self.assertEqual(source["max_streams"], 1)
        [active] = source["active"]
        self.assertEqual(active["channel_id"], backup.telemetry_channel_id(backup.order[1]))
        self.assertTrue(active["pinned"])
        self.assertFalse(active["standby"])
        self.assertEqual(active["pending_requests"], 2)
        self.assertGreaterEqual(active["idle_seconds"], 0)
        self.assertNotIn("PRIVATE", json.dumps(source))

        # Start the home stream, then stall it while the same backup remains busy.
        cid = self.base_manager.order[0]
        home = self.base_manager.active[cid].gateway
        for seq in range(100, 106):
            home.cache[seq] = (6.0, b"video")
        home.latest = 105
        home._fast_start_at = 0
        home.last_progress = home.last_segment_download = time.time()
        with self.request(token, f"channel/{cid}/index.m3u8"):
            pass
        home.last_segment_download -= 90
        self.registry.existing_failover(token, cid).last_switch -= 25
        for _ in range(2):
            occupant.accessed -= 1
            with self.request(token, f"channel/{cid}/index.m3u8"):
                pass
        telemetry.flush()
        blocked = [e for e in export_incident_rows(telemetry.path, kind="cdn_decision")
                   if e["detail"] == "NO_READY_BACKUP"]
        self.assertEqual(len(blocked), 1)
        backup_info = blocked[0]["diagnostics"]["sources"]["2"]
        self.assertEqual(backup_info["active"][0]["pending_requests"], 0)
        self.assertEqual(backup_info["active"][0]["channel_id"], active["channel_id"])

    def test_three_viewers_start_on_own_sources_and_one_may_fail_over(self):
        """Each TV keeps its own token and begins on its own Edem CDN."""
        from collections import OrderedDict
        telemetry = Telemetry(Path(self.temp.name) / "events.db")
        self.addCleanup(telemetry.close)
        self.registry.options["telemetry"] = telemetry
        tokens = {}
        ids = {}
        for slot in (1, 2, 3):
            playlist = ('#EXTM3U\n'
                        '#EXTINF:-1,VF Теория большого взрыва\n'
                        f'https://cdn{slot}.example/PRIVATE_CDN_{slot}\n')
            with patch("iptv_profiles.download_playlist", return_value=playlist):
                row, _ = self.store.update(
                    slot, f"TV-{slot}", f"https://provider.example/PRIVATE_M3U_{slot}")
            tokens[slot] = row["token"]
        self.registry.resolve(tokens[1])
        for slot in (1, 2, 3):
            manager = self.registry.resolve(tokens[slot])[0]
            ids[slot] = next(iter(manager.channels))
        muxes = {}
        for slot in (1, 2, 3):
            mux, candidates = self.registry.failover_for(tokens[slot], ids[slot])
            muxes[slot] = mux
            self.assertEqual(mux.home_slot, slot)
            self.assertEqual([item for item, _ in candidates], [slot] +
                             [other for other in (1, 2, 3) if other != slot])
            for source_slot, worker in candidates:
                gw = worker.gateway
                with gw.lock:
                    gw.cache = OrderedDict(
                        (seq, (6.0, f"CDN_{source_slot}_{seq}".encode()))
                        for seq in range(100, 106))
                    gw.latest = 105
                    gw.target = 6.0
                    gw.last_progress = gw.last_segment_download = time.time()
                    gw._fast_start_at = time.monotonic() - 2
        for slot in (1, 2, 3):
            with self.request(tokens[slot], f"channel/{ids[slot]}/index.m3u8") as response:
                playlist = response.read().decode()
            self.assertIn(f"/{tokens[slot]}/channel/{ids[slot]}/segment/{muxes[slot].generation}.1.ts", playlist)
            with self.request(tokens[slot], f"channel/{ids[slot]}/segment/{muxes[slot].generation}.1.ts") as response:
                self.assertEqual(response.read(), f"CDN_{slot}_100".encode())
            self.assertEqual(muxes[slot].source_slot, slot)
            with self.request(tokens[slot], "status") as response:
                status = json.load(response)
            self.assertEqual(status["multi_cdn"][0]["home_cabinet"], slot)
            self.assertEqual(status["multi_cdn"][0]["selected_cabinet"], slot)
            self.assertEqual(status["multi_cdn"][0]["switches"], 0)
            self.assertNotIn("PRIVATE_CDN_", json.dumps(status))
            self.assertNotIn("PRIVATE_M3U_", json.dumps(status))

        # Cabinet 1 fails. Its own viewers 2 and 3 remain on their home
        # accounts, while viewer 1 can move to one of the ready backups.
        first = self.registry.resolve(tokens[1])[0].active[ids[1]]
        with first.gateway.lock:
            first.gateway.last_progress -= 90
            first.gateway.last_segment_download -= 90
        muxes[1].last_switch -= 25
        with self.request(tokens[1], f"channel/{ids[1]}/index.m3u8") as response:
            self.assertEqual(response.status, 200)
        self.assertIn(muxes[1].source_slot, (2, 3))
        self.assertEqual(muxes[2].source_slot, 2)
        self.assertEqual(muxes[3].source_slot, 3)
        telemetry.flush()
        switches = [e for e in query_incidents(telemetry.path)["events"]
                    if e["kind"] == "cdn_decision" and e["detail"] == "SOURCE_UNAVAILABLE"]
        self.assertEqual(len(switches), 1)
        switch = switches[0]
        self.assertEqual(switch["channel_id"], ids[1])
        self.assertEqual(switch["diagnostics"]["from_slot"], 1)
        self.assertEqual(switch["diagnostics"]["selected_slot"], muxes[1].source_slot)
        self.assertFalse(switch["diagnostics"]["sources"]["1"]["fresh"])
        # No alternative can contribute fresh video. Repeated polls log one decision.
        for slot in (2, 3):
            gw = self.registry.resolve(tokens[slot])[0].active[ids[slot]].gateway
            gw.last_segment_download -= 90
        muxes[1].last_switch -= 25
        for _ in range(3):
            self.registry.publish_failover(muxes[1], tokens[1])
        telemetry.flush()
        blocked = [e for e in query_incidents(telemetry.path)["events"]
                   if e["detail"] == "NO_READY_BACKUP"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["diagnostics"]["sources"]["3"]["fresh"], False)
        self.assertNotIn("PRIVATE_", json.dumps(switches + blocked))
        # A third TV requesting a different token cannot retrieve segments
        # from someone else's virtual stream.
        self.assertIsNot(muxes[1], muxes[2])
        self.assertIsNot(muxes[2], muxes[3])
        self.assertNotIn("PRIVATE_", json.dumps(muxes[1].status()))

    def test_non_vf_channel_gets_virtual_hls_and_segments_over_original_tv_token(self):
        """All-channel path works even if only the home Edem account is active."""
        from collections import OrderedDict
        token = self.store.get(1)["token"]
        manager = self.registry.resolve(token)[0]
        cid = manager.order[1]  # Channel B, not the former VF pilot.
        mux, candidates = self.registry.failover_for(token, cid)
        self.assertEqual(mux.channel_name, "Channel B")
        self.assertEqual([slot for slot, _ in candidates], [1])
        gw = candidates[0][1].gateway
        with gw.lock:
            gw.cache = OrderedDict(
                (seq, (6.0, f"CHANNEL_B_{seq}".encode())) for seq in range(100, 106))
            gw.latest = 105
            gw.target = 6.0
            gw.last_progress = gw.last_segment_download = time.time()
            gw._fast_start_at = time.monotonic() - 2
        with self.request(token, f"channel/{cid}/index.m3u8") as response:
            manifest = response.read().decode()
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", manifest)
        self.assertIn(f"/{token}/channel/{cid}/segment/{mux.generation}.1.ts", manifest)
        with self.request(token, f"channel/{cid}/segment/{mux.generation}.1.ts") as response:
            self.assertEqual(response.read(), b"CHANNEL_B_100")
        with self.request(token, "status") as response:
            report = json.load(response)["multi_cdn"][0]
        self.assertEqual(report["channel"], "Channel B")
        self.assertEqual(report["channel_id"], cid)
        self.assertEqual(report["selected_cabinet"], 1)

    def test_dedicated_cabin_can_switch_channels_without_waiting_for_old_stream(self):
        with patch("iptv_profiles.download_playlist", return_value=PLAYLIST):
            profile, _ = self.store.update(
                2, "Вадим", "https://provider.example/private")
        manager = self.registry.resolve(profile["token"])[0]
        self.assertTrue(manager.prefer_owner_tuning)
        self.assertEqual(manager.max_streams, 1)
        first = manager.start(manager.order[0])
        self.assertIsNotNone(first)
        second = manager.start(manager.order[1])
        self.assertIsNotNone(second)
        self.assertTrue(first.gateway.stop.is_set())
        self.assertNotIn(manager.order[0], manager.active)
        self.assertIs(manager.active[manager.order[1]], second)

    def fetch_manifest(self, token, cid, player="living-room"):
        return self.fetch_tv_url(f"{self.base}/{token}/channel/{cid}/index.m3u8?device={player}")

    def fetch_tv_url(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "Identical TV"})
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read().decode()

    def wait_for_pending_manifest(self, manager, cid, response):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if response.done():
                self.fail(f"Manifest finished before warming: {response.result()}")
            with manager.lock:
                worker = manager.active.get(cid)
                if worker is not None and worker.pending_requests:
                    return worker
            time.sleep(0.01)
        self.fail("Manifest did not begin warming")

    def test_rapid_retune_supersedes_same_player_startup_with_one_slot(self):
        token = self.configure_backup(2, PLAYLIST + OTHER.split("\n", 1)[1])
        manager = self.registry.resolve(token)[0]
        manager.startup_wait = 3
        first, second, third = manager.order

        with ThreadPoolExecutor(max_workers=3) as requests:
            a = requests.submit(self.fetch_manifest, token, first)
            old = self.wait_for_pending_manifest(manager, first, a)
            b = requests.submit(self.fetch_manifest, token, second)
            interim = self.wait_for_pending_manifest(manager, second, b)
            c = requests.submit(self.fetch_manifest, token, third)
            newest = self.wait_for_pending_manifest(manager, third, c)
            self.fill_cache(newest, "latest")

            self.assertEqual(a.result(timeout=2)[0], 409)
            self.assertEqual(b.result(timeout=2)[0], 409)
            status, manifest = c.result(timeout=2)
            self.assertEqual(status, 200)
            self.assertIn(f"/channel/{third}/segment/", manifest)

        self.assertTrue(old.gateway.stop.is_set())
        self.assertTrue(interim.gateway.stop.is_set())
        self.assertEqual(list(manager.active), [third])
        self.assertEqual(manager.stream_limit_denials, 0)
        self.assertEqual(manager.warmup_timeouts, 0)

    def test_pending_manifest_of_another_player_keeps_its_slot(self):
        token = self.configure_backup(2)
        manager = self.registry.resolve(token)[0]
        manager.startup_wait = 3
        first, second = manager.order

        with ThreadPoolExecutor(max_workers=1) as requests:
            a = requests.submit(self.fetch_manifest, token, first)
            owner = self.wait_for_pending_manifest(manager, first, a)
            self.assertEqual(self.fetch_manifest(token, second, "kitchen")[0], 503)
            self.assertFalse(owner.gateway.stop.is_set())
            self.fill_cache(owner, "owner")
            self.assertEqual(a.result(timeout=2)[0], 200)
        self.assertEqual(manager.stream_limit_denials, 1)

    def test_identical_legacy_signatures_cannot_cancel_an_inflight_warmup(self):
        token = self.configure_backup(2)
        manager = self.registry.resolve(token)[0]
        manager.startup_wait = 3
        first, second = manager.order
        first_url = f"{self.base}/{token}/channel/{first}/index.m3u8"
        second_url = f"{self.base}/{token}/channel/{second}/index.m3u8"
        with ThreadPoolExecutor(max_workers=1) as requests:
            pending = requests.submit(self.fetch_tv_url, first_url)
            owner = self.wait_for_pending_manifest(manager, first, pending)
            self.assertEqual(self.fetch_tv_url(second_url)[0], 503)
            self.assertFalse(owner.gateway.stop.is_set())
            self.fill_cache(owner, "owner")
            self.assertEqual(pending.result(timeout=2)[0], 200)

    def test_a_former_second_player_does_not_block_the_continuing_player(self):
        token = self.configure_backup(2)
        backup_token = self.configure_backup(3)
        manager = self.registry.resolve(token)[0]
        backup = self.registry.resolve(backup_token)[0]
        first, second = manager.order
        self.fill_cache(manager.start(first), "first")
        base = time.monotonic()
        elapsed = 0

        with patch("iptv_profile_gateway.time", SimpleNamespace(monotonic=lambda: base + elapsed)):
            self.assertEqual(self.fetch_manifest(token, first)[0], 200)
            self.assertEqual(self.fetch_manifest(token, first, "kitchen")[0], 200)
            self.assertEqual(self.fetch_manifest(token, second)[0], 503)
            elapsed = 35
            self.assertEqual(self.fetch_manifest(token, first)[0], 200)
            elapsed = 46
            self.fill_cache(backup.start(second), "reserve")
            self.assertEqual(self.fetch_manifest(token, second)[0], 200)

    def test_ready_backup_serves_manifest_and_segments_if_home_slot_is_busy(self):
        token = self.configure_backup(2)
        backup_token = self.configure_backup(3)
        manager = self.registry.resolve(token)[0]
        backup = self.registry.resolve(backup_token)[0]
        first, second = manager.order
        occupant = manager.start(first, pinned=True)
        self.fill_cache(backup.start(second), "reserve")

        status, manifest = self.fetch_manifest(token, second)
        self.assertEqual(status, 200)
        segment = next(line for line in manifest.splitlines() if line.startswith("/"))
        with urllib.request.urlopen(self.base + segment, timeout=3) as response:
            self.assertEqual(response.read(), b"reserve-3")
        self.assertIs(manager.active[first], occupant)
        self.assertFalse(occupant.gateway.stop.is_set())
        self.assertNotIn(second, manager.active)
        self.assertEqual(manager.stream_limit_denials, 0)

    def test_device_catalog_carries_identity_and_old_requests_cannot_retune(self):
        token = self.configure_backup(2)
        manager = self.registry.resolve(token)[0]
        manager.startup_wait = 3
        first, second = manager.order
        with self.request(token, "channels.m3u?device=living-room") as response:
            paths = [line for line in response.read().decode().splitlines() if line.startswith("https://")]
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.endswith("?device=living-room") for path in paths))
        self.fill_cache(manager.start(first), "first")
        first_url = f"{self.base}/{token}/channel/{first}/index.m3u8?device=living-room"
        with urllib.request.urlopen(first_url, timeout=3) as response:
            old_session = response.geturl()
        self.assertIn("&session=", old_session)
        self.assertEqual(self.fetch_tv_url(old_session.replace("device=living-room", "device=kitchen"))[0], 409)

        with ThreadPoolExecutor(max_workers=1) as requests:
            next_channel = requests.submit(self.fetch_manifest, token, second)
            newest = self.wait_for_pending_manifest(manager, second, next_channel)
            self.assertEqual(self.fetch_tv_url(old_session)[0], 409)
            self.fill_cache(newest, "second")
            self.assertEqual(next_channel.result(timeout=2)[0], 200)
        self.assertEqual(list(manager.active), [second])
        self.assertEqual(manager.stream_limit_denials, 0)
        with ThreadPoolExecutor(max_workers=1) as requests:
            reopen = requests.submit(self.fetch_tv_url, first_url)
            newest = self.wait_for_pending_manifest(manager, first, reopen)
            self.assertEqual(self.fetch_tv_url(old_session)[0], 409)
            self.fill_cache(newest, "reopened")
            self.assertEqual(reopen.result(timeout=2)[0], 200)
        self.assertEqual(list(manager.active), [first])

    def test_redirected_manifest_resumes_after_idle_and_gateway_restart(self):
        token = self.configure_backup(2)
        manager = self.registry.resolve(token)[0]
        first, second = manager.order
        self.fill_cache(manager.start(first), "original")
        with self.request(token, f"channel/{first}/index.m3u8?device=tv") as response:
            obsolete = response.geturl()
        self.assertEqual(self.fetch_manifest(token, second, "tv")[0], 503)
        self.fill_cache(manager.active[second], "second")
        with self.request(token, f"channel/{second}/index.m3u8?device=tv") as response:
            current = response.geturl()
            old_manifest = response.read().decode()

        # Expire both the tuning and its cached video. The player only has its final URL.
        self.registry.failovers[2, second].last_request -= 90
        self.fill_cache(manager.active[second], "after-idle")
        self.assertEqual(self.fetch_tv_url(obsolete)[0], 409)
        status, resumed = self.fetch_tv_url(current)
        self.assertEqual(status, 200)

        # A new registry and HTTP handler share only the on-disk profile directory.
        self.registry.close()
        self.registry = ProfileRegistry(self.store, self.base_manager, self.legacy_token.read_text(),
                                        self.legacy_epg, dict(max_streams=2, startup_wait=3))
        self.server.RequestHandlerClass = make_multi_handler(
            self.base_manager, self.legacy_token.read_text(), profile_registry=self.registry)
        manager = self.registry.resolve(token)[0]
        self.assertEqual(self.fetch_tv_url(obsolete)[0], 409)
        self.assertEqual(self.fetch_tv_url(current.replace("device=tv", "device=other"))[0], 409)
        self.assertFalse(manager.active)
        with ThreadPoolExecutor(max_workers=1) as requests:
            pending = requests.submit(self.fetch_tv_url, current)
            worker = self.wait_for_pending_manifest(manager, second, pending)
            self.fill_cache(worker, "after-restart")
            status, recovered = pending.result(timeout=2)
        self.assertEqual(status, 200)

        def window(manifest):
            start = int(next(line.split(":")[1] for line in manifest.splitlines()
                             if line.startswith("#EXT-X-MEDIA-SEQUENCE:")))
            return range(start, start + manifest.count("#EXTINF:"))

        self.assertGreater(window(resumed).start, window(old_manifest)[-1])
        self.assertGreater(window(recovered).start, window(resumed)[-1])
        segment = next(line for line in recovered.splitlines() if line.endswith(".ts"))
        with urllib.request.urlopen(self.base + segment, timeout=3) as response:
            self.assertTrue(response.read())

    def test_disabling_multicdn_allows_the_native_launch_of_an_old_channel(self):
        token = self.configure_backup(2)
        manager = self.registry.resolve(token)[0]
        first, second = manager.order
        self.fill_cache(manager.start(first), "first")
        self.assertEqual(self.fetch_manifest(token, first)[0], 200)
        self.assertEqual(self.fetch_manifest(token, second)[0], 503)

        (self.store.directory / "multicdn.disabled").touch()
        status, message = self.fetch_manifest(token, first)
        self.assertEqual(status, 503)
        self.assertIn("Buffer warming up", message)
        self.assertEqual(list(manager.active), [first])

    def test_retune_preserves_a_home_worker_serving_another_cabinet(self):
        token = self.configure_backup(2)
        backup_token = self.configure_backup(3)
        primary = self.store.get(1)["token"]
        manager = self.registry.resolve(token)[0]
        backup = self.registry.resolve(backup_token)[0]
        first, second = manager.order
        old = manager.start(first)
        self.fill_cache(old, "shared")

        self.assertEqual(self.fetch_manifest(token, first)[0], 200)
        self.assertEqual(self.fetch_manifest(primary, first)[0], 200)
        self.assertEqual(self.registry.existing_failover(primary, first).source_slot, 2)
        self.fill_cache(backup.start(second), "reserve")

        self.assertEqual(self.fetch_manifest(token, second)[0], 200)
        self.assertEqual(self.registry.existing_failover(token, second).source_slot, 3)
        self.assertIs(manager.active[first], old)
        self.assertFalse(old.gateway.stop.is_set())

        # A viewer on another cabinet still renews the borrowed worker even
        # after the home TV has switched away from it.
        with manager.lock:
            old.accessed -= manager.idle_seconds + 1
        self.assertEqual(self.fetch_manifest(primary, first)[0], 200)
        manager.sweep()
        self.assertIs(manager.active.get(first), old)
        self.assertEqual(self.registry.existing_failover(primary, first).source_slot, 2)

    def test_unadvanced_reserve_does_not_replace_a_playing_channel(self):
        token = self.configure_backup(2, PLAYLIST + OTHER.split("\n", 1)[1])
        manager = self.registry.resolve(token)[0]
        first, second, pinned = manager.order
        manager.start(pinned, pinned=True)
        self.fill_cache(self.base_manager.start(first), "first")
        self.fill_cache(self.base_manager.start(second), "second")

        self.assertEqual(self.fetch_manifest(token, first)[0], 200)
        self.assertEqual(self.fetch_manifest(token, second)[0], 200)
        self.assertEqual(self.fetch_manifest(token, first)[0], 503)
        self.assertEqual(self.registry.tunings[2].mux.channel_id, second)
        self.assertEqual(self.fetch_manifest(token, second)[0], 200)

    def test_reserve_cache_reset_during_retune_keeps_the_playing_channel(self):
        token = self.configure_backup(2, PLAYLIST + OTHER.split("\n", 1)[1])
        manager = self.registry.resolve(token)[0]
        first, second, pinned = manager.order
        manager.start(pinned, pinned=True)
        self.fill_cache(self.base_manager.start(first), "playing")
        reserve = self.base_manager.start(second)
        self.fill_cache(reserve, "reserve")
        self.assertEqual(self.fetch_manifest(token, first)[0], 200)

        candidates = self.registry._reserves

        def reset_cache_after_discovery(*args, **kwargs):
            result = candidates(*args, **kwargs)
            with reserve.gateway.lock:
                reserve.gateway.cache.clear()
                reserve.gateway.cache_epoch += 1
            return result

        with patch.object(self.registry, "_reserves", side_effect=reset_cache_after_discovery):
            self.assertEqual(self.fetch_manifest(token, second)[0], 503)
        self.assertEqual(self.registry.tunings[2].mux.channel_id, first)
        self.assertEqual(self.fetch_manifest(token, first)[0], 200)

    def test_guest_keeps_playing_a_worker_after_the_home_tuning_expired(self):
        token = self.configure_backup(2)
        primary = self.store.get(1)["token"]
        manager = self.registry.resolve(token)[0]
        first = manager.order[0]
        old = manager.start(first)
        self.fill_cache(old, "shared")
        self.assertEqual(self.fetch_manifest(token, first)[0], 200)
        with self.registry.lock:
            self.registry.tunings[2].mux.last_request -= 46

        self.assertEqual(self.fetch_manifest(primary, first)[0], 200)
        self.assertNotIn(2, self.registry.tunings)
        self.assertEqual(self.registry.existing_failover(primary, first).source_slot, 2)
        with manager.lock:
            old.accessed -= manager.idle_seconds + 1
        self.assertEqual(self.fetch_manifest(primary, first)[0], 200)
        manager.sweep()
        self.assertIs(manager.active.get(first), old)

    def test_every_channel_is_eligible_and_uses_matching_backup_only(self):
        """A movie, news and UHD channel are equal citizens of failover."""
        primary = ("#EXTM3U\n"
                   "#EXTINF:-1,Первый канал UHD\nhttps://cdn1.example/uhd\n"
                   "#EXTINF:-1,Кинокомедия HD\nhttps://cdn1.example/movies\n"
                   "#EXTINF:-1,Channel Not Shared\nhttps://cdn1.example/unique\n")
        second = ("#EXTM3U\n"
                  "#EXTINF:-1,Первый канал UHD\nhttps://cdn2.example/uhd\n"
                  "#EXTINF:-1,Кинокомедия HD\nhttps://cdn2.example/movies\n"
                  "#EXTINF:-1,Channel Not Shared (not same show)\nhttps://cdn2.example/unique\n")
        third = ("#EXTM3U\n"
                 "#EXTINF:-1,Первый канал UHD\nhttps://cdn3.example/uhd\n"
                 "#EXTINF:-1,Кинокомедия HD (SD)\nhttps://cdn3.example/movies\n")
        for slot, playlist in enumerate((primary, second, third), start=1):
            with patch("iptv_profiles.download_playlist", return_value=playlist):
                self.store.update(slot, f"TV {slot}", f"https://provider.example/account{slot}")
        token = self.store.get(1)["token"]
        manager = self.registry.resolve(token)[0]
        by_name = {channel.name: channel.id for channel in manager.channels.values()}

        # All channels, not just VF, receive a private virtual HLS timeline.
        for name, slots in (("Первый канал UHD", [1, 2, 3]), ("Кинокомедия HD", [1, 2]),
                            ("Channel Not Shared", [1])):
            cid = by_name[name]
            mux, candidate_streams = self.registry.failover_for(token, cid)
            self.assertEqual(mux.channel_name, name)
            self.assertEqual(mux.channel_id, cid)
            self.assertEqual(mux.home_slot, 1)
            self.assertEqual([slot for slot, _ in candidate_streams], slots)
            self.assertTrue(all(worker.channel.name == name for _, worker in candidate_streams))
        # No upstream URL leaves the status interface.
        with self.request(token, "status") as response:
            data = json.load(response)
        self.assertEqual({row["channel"] for row in data["multi_cdn"]},
                         {"Первый канал UHD", "Кинокомедия HD", "Channel Not Shared"})
        self.assertNotIn("cdn1.example", json.dumps(data))

    def test_duplicate_names_with_different_groups_never_select_random_backup(self):
        from iptv_multi import channels_from_entries
        source = channels_from_entries([
            ("Кино", "HD", "https://example.org/1"),
            ("Кино", "SD", "https://example.org/2"),
        ])
        target = source[0]
        ambiguous = channels_from_entries([
            ("Кино", "Other", "https://example.org/3"),
            ("Кино", "Other", "https://example.org/4"),
        ])
        owner = MultiGateway(source, self.legacy)
        backup = MultiGateway(ambiguous, self.legacy)
        self.assertIsNone(self.registry._matching_channel_id(owner, backup, target))
        self.assertEqual(self.registry._matching_channel_id(owner, owner, target), target.id)

    def configure_backup(self, slot, playlist=PLAYLIST):
        with patch("iptv_profiles.download_playlist", return_value=playlist):
            row, _ = self.store.update(slot, f"TV {slot}", f"https://provider.example/{slot}")
        self.registry.resolve(row["token"])
        return row["token"]

    @staticmethod
    def fill_cache(worker, prefix):
        gw = worker.gateway
        with gw.lock:
            gw.cache.clear()
            for seq in range(6):
                gw.cache[seq] = (6.0, f"{prefix}-{seq}".encode())
            gw.latest = 5
            gw.delay = gw.startup_reserve = 0
            gw.last_progress = gw.last_segment_download = time.time()
            gw._fast_start_at = 0

    def test_retune_releases_both_unused_standbys_immediately(self):
        for slot in (2, 3):
            self.configure_backup(slot)
        token = self.store.get(1)["token"]
        first, second = self.base_manager.order
        _, old = self.registry.failover_for(token, first)
        _, new = self.registry.failover_for(token, second)
        self.assertEqual([slot for slot, _ in new], [1, 2, 3])
        self.assertTrue(all(worker.gateway.stop.is_set() for _, worker in old[1:]))

    def test_retune_keeps_shared_standby_and_other_home_viewer(self):
        second_token = self.configure_backup(2)
        self.configure_backup(3)
        token = self.store.get(1)["token"]
        first, second = self.base_manager.order
        _, old = self.registry.failover_for(token, first)
        _, shared = self.registry.failover_for(second_token, first)
        self.assertIs(dict(old)[3], dict(shared)[3])
        _, new = self.registry.failover_for(token, second)
        self.assertEqual([slot for slot, _ in new], [1])
        self.assertFalse(dict(shared)[2].gateway.stop.is_set())
        self.assertFalse(dict(shared)[3].gateway.stop.is_set())
        _, following = self.registry.failover_for(second_token, second)
        self.assertIn(3, dict(following))
        self.assertTrue(dict(shared)[3].gateway.stop.is_set())

    def test_retune_frees_guest_place_for_another_viewer(self):
        second_token = self.configure_backup(2)
        third_token = self.configure_backup(3)
        first, second = self.base_manager.order
        primary = self.store.get(1)["token"]
        third = self.registry.resolve(third_token)[0]
        self.fill_cache(third.start(first), "third")
        route = f"channel/{first}/index.m3u8"
        with self.request(primary, route) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.registry.existing_failover(primary, first).source_slot, 3)
        self.registry.failover_for(primary, second)
        with self.request(second_token, route) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.registry.existing_failover(second_token, first).source_slot, 3)
        report = self.admin_state().gateway_status()["multi_cdn"]
        self.assertEqual(report[0]["source_viewer_load"]["3"], 1)

    def test_expired_segment_url_cannot_read_native_or_reopened_timeline(self):
        token = self.store.get(1)["token"]
        cid = self.base_manager.order[0]
        worker = self.base_manager.start(cid)
        self.fill_cache(worker, "old")
        route = f"channel/{cid}/index.m3u8"

        def first_segment():
            with self.request(token, route) as response:
                return next(line for line in response.read().decode().splitlines()
                            if line.startswith("/"))

        def assert_expired(url):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base + url)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

        old_url = first_segment()
        with urllib.request.urlopen(self.base + old_url) as response:
            self.assertEqual(response.read(), b"old-3")
        self.registry.existing_failover(token, cid).last_request -= 100
        with self.request(token, "status"):
            pass
        assert_expired(old_url)
        self.fill_cache(worker, "new")
        new_url = first_segment()
        self.assertNotEqual(new_url, old_url)
        assert_expired(old_url)
        with urllib.request.urlopen(self.base + new_url) as response:
            self.assertEqual(response.read(), b"new-3")
        (self.store.directory / "multicdn.disabled").touch()
        assert_expired(new_url)
        raw_url = first_segment()
        with urllib.request.urlopen(self.base + raw_url) as response:
            self.assertTrue(response.read().startswith(b"new-"))

    def test_idle_tuning_cannot_republish_same_worker_window(self):
        token = self.store.get(1)["token"]
        cid = self.base_manager.order[0]
        worker = self.base_manager.start(cid)
        self.fill_cache(worker, "old")
        route = f"channel/{cid}/index.m3u8"
        with self.request(token, route) as response:
            self.assertEqual(response.status, 200)
        self.registry.existing_failover(token, cid).last_request -= 50
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(token, route)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()

    def test_inline_regions_match_across_reordered_catalogues_and_keep_public_ids(self):
        from iptv_multi import channels_from_entries
        def regional_playlist(regions):
            return "#EXTM3U\n" + "".join(
                f'#EXTINF:-1 group-title="{region}",News\nhttps://provider.example/{region}\n'
                for region in regions)
        home = self.configure_backup(1, regional_playlist(["East", "West"]))
        self.configure_backup(2, regional_playlist(["West", "East"]))
        manager = self.registry.resolve(home)[0]
        legacy_ids = [c.id for c in channels_from_entries([
            ("News", "", "https://provider.example/East"),
            ("News", "", "https://provider.example/West")])]
        self.assertEqual(manager.order, legacy_ids)
        for cid, region in zip(legacy_ids, ("East", "West")):
            _, sources = self.registry.failover_for(home, cid)
            self.assertEqual(dict(sources)[2].channel.url, f"https://provider.example/{region}")
            self.assertEqual(manager.channels[cid].group, region)

    def test_duplicate_same_name_and_group_in_either_catalogue_skips_backup(self):
        entry = '#EXTINF:-1 group-title="East",News\nhttps://provider.example/news\n'
        for home_count, backup_count in ((2, 1), (1, 2), (2, 2)):
            with self.subTest(home=home_count, backup=backup_count):
                home = self.configure_backup(1, "#EXTM3U\n" + entry * home_count)
                self.configure_backup(2, "#EXTM3U\n" + entry * backup_count)
                manager = self.registry.resolve(home)[0]
                for cid in manager.order:
                    _, sources = self.registry.failover_for(home, cid)
                    self.assertEqual([slot for slot, _ in sources], [1])
                    self.assertEqual(sources[0][1].channel.id, cid)

    def test_refresh_reordering_inline_regions_cannot_reuse_wrong_live_worker(self):
        east = '#EXTINF:-1 group-title="East",News\nhttps://provider.example/east\n'
        west = east.replace("East", "West").replace("east", "west")
        original = "#EXTM3U\n" + east + west
        home = self.configure_backup(1, original)
        second = self.configure_backup(2, original)
        manager = self.registry.resolve(second)[0]
        manager.start(manager.order[0])
        with patch("iptv_profile_jobs.download_playlist", return_value="#EXTM3U\n" + west + east):
            generate_for_slot(self.store, 2, refresh_m3u=True, m3u_only=True)
        owner = self.registry.resolve(home)[0]
        _, sources = self.registry.failover_for(home, owner.order[1])
        self.assertEqual(dict(sources)[2].channel.url, "https://provider.example/west")

    def test_idle_tv_status_hides_standby_that_admin_can_inspect(self):
        home = self.store.get(1)["token"]
        idle = self.configure_backup(2)
        cid = self.base_manager.order[0]
        self.registry.failover_for(home, cid)
        for headers in ({}, {"Authorization": f"Bearer {idle}"}):
            req = urllib.request.Request(f"{self.base}/{idle}/status", headers=headers)
            with urllib.request.urlopen(req) as response:
                report = json.load(response)
            self.assertEqual(report["streams"], [])
            self.assertEqual(report["active_streams"], 0)
            self.assertNotIn(cid, json.dumps(report))
            self.assertNotIn("Channel A", json.dumps(report))
        status = self.admin_state().gateway_status()
        details = [row for row in status["streams"] if row["profile_slot"] == 2]
        self.assertEqual([(row["id"], row["standby"]) for row in details], [(cid, True)])

    def admin_state(self):
        from iptv_admin import AdminState
        return AdminState(self.legacy, self.legacy_epg, self.server.server_port,
                          self.legacy_token, "admin-test-password", profiles_dir=self.store.directory)

    def test_monitor_attributes_shared_source_to_its_viewers_and_respects_guest_place(self):
        tokens = [self.store.get(1)["token"], self.configure_backup(2), self.configure_backup(3)]
        cid = self.base_manager.order[0]
        _, workers = self.registry.failover_for(tokens[1], cid)
        self.fill_cache(dict(workers)[3], "third")
        route = f"channel/{cid}/index.m3u8"
        with self.request(tokens[1], route):
            pass
        self.fill_cache(dict(workers)[1], "first")
        with self.request(tokens[0], route):
            pass
        admin = self.admin_state()
        for expected_viewers in ([2], [2, 3]):
            if 3 in expected_viewers:
                with self.request(tokens[2], route):
                    pass
            data = admin.overview()
            first = next(row for row in data["multi_cdn"] if row["home_cabinet"] == 1)
            third = next(s for s in first["sources"] if s["cabinet"] == 3)
            self.assertTrue(third["guest_full"])
            self.assertTrue(third["fresh"])
            self.assertEqual(first["source_viewer_load"]["3"], len(expected_viewers))
            stream = next(s for s in data["streams"] if s["profile_slot"] == 3)
            self.assertEqual(stream["viewers"], expected_viewers)
            self.assertEqual(stream["standby"], 3 not in expected_viewers)
        with self.request(tokens[0], "status?scope=cabinets") as response:
            private = json.load(response)
        self.assertNotIn("cabinet_statuses", private)
        self.assertNotIn("guest_full", json.dumps(private))

    def test_admin_channel_history_outlives_video_and_resumes_without_duplicate_events(self):
        token = self.store.get(1)["token"]
        self.configure_backup(2)
        cid = self.base_manager.order[0]
        mux, workers = self.registry.failover_for(token, cid)
        for _, worker in workers:
            self.fill_cache(worker, "video")
        route = f"channel/{cid}/index.m3u8"
        with self.request(token, route):
            pass
        workers[0][1].gateway.last_segment_download -= 60
        mux.last_switch -= 30
        with self.request(token, route):
            pass
        admin = self.admin_state()
        before = admin.overview()["multi_cdn"][0]
        self.assertEqual(before["switches"], 1)
        mux.last_request -= 71
        rows = admin.overview()["multi_cdn"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["switch_history"], before["switch_history"])
        self.assertFalse(rows[0]["tuned"])
        self.assertIsNone(self.registry.existing_failover(token, cid))
        self.fill_cache(workers[0][1], "new")
        with self.request(token, route):
            pass
        current, = admin.overview()["multi_cdn"]
        self.assertEqual(current["switches"], 1)
        self.assertEqual(current["switch_history"][1:], before["switch_history"])

    def test_old_channel_mux_is_reclaimed_to_limit_cache_growth(self):
        playlist = ("#EXTM3U\n"
                    "#EXTINF:-1,News\nhttps://cdn.example/news\n"
                    "#EXTINF:-1,Movies\nhttps://cdn.example/movies\n")
        with patch("iptv_profiles.download_playlist", return_value=playlist):
            self.store.update(1, "Основной", "https://provider.example/playlist")
        token = self.store.get(1)["token"]
        manager = self.registry.resolve(token)[0]
        ids = list(manager.channels)
        old_mux, _ = self.registry.failover_for(token, ids[0])
        old_mux.last_request -= 100
        new_mux, _ = self.registry.failover_for(token, ids[1])
        self.assertIsNot(new_mux, old_mux)
        self.assertIsNone(self.registry.existing_failover(token, ids[0]))
        self.assertIs(self.registry.existing_failover(token, ids[1]), new_mux)

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
        secondary_id = next(iter(self.registry.resolve(backup_token)[0].channels))
        secondary_pilot = self.registry.failover_for(backup_token, secondary_id)
        self.assertIsNotNone(secondary_pilot)
        self.assertEqual(secondary_pilot[0].home_slot, 2)
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
        self.assertIn(f"/{owner_token}/channel/{cid}/segment/{mux.generation}.1.ts", first_manifest)
        self.assertNotIn("PRIVATE_1", first_manifest)
        self.assertNotIn("M3U_KEY_1", first_manifest)
        with self.request(owner_token, f"channel/{cid}/segment/{mux.generation}.1.ts") as response:
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
        self.assertIn(f"/{owner_token}/channel/{cid}/segment/{mux.generation}.4.ts", next_manifest)
        with self.request(owner_token, f"channel/{cid}/segment/{mux.generation}.4.ts") as response:
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
