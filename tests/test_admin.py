"""Offline integration tests for isolated HeySmart TV admin service."""
import gzip
import csv
import io
import http.cookiejar
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

import iptv_admin as admin
from iptv_telemetry import Telemetry


PLAYLIST = """#EXTM3U x-tvg-url="https://provider.test/secret/xmltv.xml.gz"
#EXTINF:-1 group-title="News",Россия 24 HD
https://example.test/SECRET_TOKEN/stream
#EXTINF:-1 group-title="Movies",ТВ 3
https://example.test/SECRET_TOKEN/other
"""
GUIDE = """<tv><channel id="test"><display-name>Россия 24</display-name></channel>
<programme channel="{id}" start="20261122000000 +0000" stop="20261122010000 +0000">
<title>Новости</title></programme></tv>"""


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.playlist = root / "playlist.m3u"
        self.playlist.write_text(PLAYLIST, encoding="utf-8")
        self.epg = root / "epg.xml.gz"
        first = admin.channels_from_text(PLAYLIST)[0]
        self.epg.write_bytes(gzip.compress(GUIDE.format(id=first.id).encode()))
        self.pwd = "a-private-password-of-sufficient-length"
        self.state = admin.AdminState(self.playlist, self.epg, 8787,
                                      root / "private_token", self.pwd,
                                      root / "usage.sqlite3")
        self._status = {
            "channels": 2, "active_streams": 1, "max_streams": 3,
            "delay_seconds": 90, "catalog_requests": 12,
            "streams": [{"id": first.id, "channel": "Россия 24 HD",
                         "ready": True, "cached_mib": 72,
                         "last_error": None,
                         "upstream_url": "PRIVATE_SECRET_NEVER_EXPOSE"}],
        }
        self.status_patch = patch.object(self.state, "gateway_status",
                                         lambda: self._status)
        self.status_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          admin.make_admin_handler(self.state, secure_cookies=False))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        self.status_patch.stop()
        self.tmp.cleanup()

    def request(self, path, *, data=None, json_body=False):
        headers = {"Content-Type": "application/json"} if json_body else {}
        req = urllib.request.Request(self.url + path, data=data, headers=headers)
        return self.opener.open(req, timeout=4)

    def login(self, password=None):
        payload = json.dumps({"password": password or self.pwd}).encode()
        return self.request("/admin/api/login", data=payload, json_body=True)

    def test_pages_load_without_exposing_credentials(self):
        with self.request("/admin/") as response:
            html = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("HeySmart TV", html)
            self.assertNotIn(self.pwd, html)
            self.assertNotIn("SECRET_TOKEN", html)
        with self.request("/admin/app.js") as response:
            self.assertIn("refresh", response.read().decode())

    def test_data_requires_separate_login(self):
        for path in ("/admin/api/session", "/admin/api/overview", "/admin/api/channels"):
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.request(path)
            self.assertEqual(result.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.login("wrong-private-password")
        self.assertEqual(result.exception.code, 401)
        with self.login() as response:
            self.assertEqual(response.status, 200)
        with self.request("/admin/api/session") as response:
            self.assertEqual(response.status, 200)

    def test_sanitized_dashboard_and_channel_search_metadata(self):
        self.login().close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.request("/admin/api/overview") as response:
                data = json.load(response)
            if data["epg"]["status"] == "ready":
                break
            time.sleep(0.05)
        self.assertEqual(data["epg"]["status"], "ready")
        self.assertEqual(data["epg"]["covered"], 1)
        self.assertEqual(data["epg"]["programmes"], 1)
        self.assertEqual(data["channels"], 2)
        self.assertEqual(data["active_streams"], 1)
        self.assertNotIn("SECRET_TOKEN", json.dumps(data))
        self.assertNotIn("PRIVATE_SECRET_NEVER_EXPOSE", json.dumps(data))
        with self.request("/admin/api/channels") as response:
            channels = json.load(response)["channels"]
        self.assertEqual([c["epg"] for c in channels], [True, False])
        self.assertEqual([c["active"] for c in channels], [True, False])
        self.assertNotIn("SECRET_TOKEN", json.dumps(channels))
        self.assertEqual([c["group"] for c in channels], ["News", "Movies"])

    def test_vps_and_iptv_metrics_require_admin_login_and_never_leak_private_urls(self):
        sample = {
            "vps": {"cpu_percent": 42.0, "cores": 2, "load_1m": 0.7,
                    "memory": {"percent": 52.0, "used_bytes": 520, "total_bytes": 1000},
                    "disk": {"percent": 33.0, "used_bytes": 330, "total_bytes": 1000},
                    "rx_bytes_per_second": 1200, "tx_bytes_per_second": 3500},
            "service": {
                "gateway": {"cpu_percent": 15.5, "rss_bytes": 456789},
                "admin": {"cpu_percent": 1.0, "rss_bytes": 123456}}
        }
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/admin/api/overview")
        self.assertEqual(denied.exception.code, 401)
        with patch.object(self.state.resource_sampler, "snapshot", return_value=sample):
            self.login().close()
            with self.request("/admin/api/overview") as response:
                body = response.read().decode()
                data = json.loads(body)
        self.assertEqual(data["resources"]["vps"]["cpu_percent"], 42.0)
        self.assertEqual(data["resources"]["service"]["gateway"]["rss_bytes"], 456789)
        self.assertEqual(data["cached_mib"], 72.0)
        self.assertEqual(data["stalled_streams"], 0)
        self.assertNotIn("PRIVATE_SECRET_NEVER_EXPOSE", body)
        self.assertNotIn("SECRET_TOKEN", body)
        with self.request("/admin/") as response:
            html = response.read().decode()
        self.assertIn('id="vps-cpu"', html)
        self.assertIn('id="service-cpu"', html)
        self.assertIn('id="service-cabins"', html)

    def test_gateway_status_aggregates_first_and_second_remote_cabinet(self):
        first_token = self.state.profiles.get(1)["token"]
        second_playlist = PLAYLIST.replace("Россия 24 HD", "Родительский")
        with patch("iptv_profiles.download_playlist", return_value=second_playlist):
            second, changed = self.state.profiles.update(
                2, "Родители", "https://provider.example/M3U_SECRET")
        self.assertTrue(changed)
        token_status = {
            first_token: {
                "channels": 2, "max_streams": 3, "delay_seconds": 60,
                "active_streams": 1, "catalog_requests": 5,
                "streams": [{"id": "channel-1", "channel": "Теория",
                             "ready": True, "cached_mib": 25}]},
            second["token"]: {
                "channels": 2, "max_streams": 1, "delay_seconds": 60,
                "active_streams": 1, "catalog_requests": 1,
                "streams": [{"id": "channel-2", "channel": "Детский",
                             "ready": True, "cached_mib": 15}]},
        }
        class Response(io.BytesIO):
            status = 200

        def mocked_open(url, timeout):
            token = url.split("/")[3]
            self.assertEqual(timeout, 1.5)
            self.assertIn(token, token_status)
            return Response(json.dumps(token_status[token]).encode())

        with patch("iptv_admin.urllib.request.urlopen", side_effect=mocked_open):
            result = admin.AdminState.gateway_status(self.state)
        self.assertIsNotNone(result)
        self.assertEqual(result["active_streams"], 2)
        self.assertEqual(result["max_streams"], 4)
        self.assertEqual(result["catalog_requests"], 6)
        self.assertEqual([row["profile_name"] for row in result["streams"]],
                         ["Основной", "Родители"])
        self.assertEqual(len(result["cabins"]), 2)
        self.assertNotIn("M3U_SECRET", json.dumps(result))
        self.assertNotIn(first_token, json.dumps(result))
        self.assertNotIn(second["token"], json.dumps(result))

    def test_monitor_aggregates_three_independent_cdn_sessions(self):
        tokens = {1: self.state.profiles.get(1)["token"]}
        for slot, name in ((2, "Вадим"), (3, "Кристина")):
            with patch("iptv_profiles.download_playlist", return_value=PLAYLIST):
                row, _ = self.state.profiles.update(
                    slot, name, f"https://provider.example/SECRET_{slot}")
            tokens[slot] = row["token"]

        def fake_status(slot):
            return {
                "channels": 2, "max_streams": 1,
                "delay_seconds": 60, "streams": [],
                "multi_cdn": [{
                    "home_cabinet": slot,
                    "selected_cabinet": slot,
                    "switches": 0,
                    "seconds_since_last_probe": 2.0,
                    "sources": [{"cabinet": slot, "fresh": True}],
                }],
                "multi_cdn_diagnostics": {
                    "enabled": True, "home_cabinet": slot,
                    "primary_channel_found": True, "cabins": [
                        {"slot": other, "configured": True, "channel_found": True,
                         **({"playing": slot == 2} if other == slot else {})}
                        for other in (1, 2, 3)],
                },
            }

        class Response(io.BytesIO):
            status = 200

        def fake_open(url, timeout):
            token = url.split("/")[3]
            slot = next(slot for slot, own in tokens.items() if own == token)
            return Response(json.dumps(fake_status(slot)).encode())

        with patch("iptv_admin.urllib.request.urlopen", side_effect=fake_open):
            data = admin.AdminState.gateway_status(self.state)
        self.assertEqual({row["home_cabinet"] for row in data["multi_cdn"]},
                         {1, 2, 3})
        self.assertEqual({row["selected_cabinet"] for row in data["multi_cdn"]},
                         {1, 2, 3})
        self.assertEqual(data["multi_cdn"][0]["source_viewer_load"],
                         {"1": 1, "2": 1, "3": 1})
        self.assertEqual(data["multi_cdn_diagnostics_by_cabin"]["2"]["home_cabinet"], 2)
        self.assertEqual(data["multi_cdn_diagnostics_by_cabin"]["3"]["cabins"][1]["name"],
                         "Вадим")
        for report in data["multi_cdn_diagnostics_by_cabin"].values():
            self.assertEqual([row["playing"] for row in report["cabins"]], [False, True, False])
        self.assertNotIn("SECRET_", json.dumps(data))
        for token in tokens.values():
            self.assertNotIn(token, json.dumps(data))
        with self.request("/admin/") as response:
            page = response.read().decode()
        self.assertIn('id="cdn-cabin-switch"', page)

    def test_cdn_monitor_html_is_auth_safe_and_api_exposes_switches(self):
        with self.request("/admin/") as response:
            page = response.read().decode()
        self.assertIn('id="view-cdn"', page)
        self.assertIn('id="cdn-monitor-sources"', page)
        self.assertIn('id="cdn-monitor-events"', page)
        self.assertIn('data-target="cdn"', page)
        self.assertNotIn("PRIVATE_SECRET_NEVER_EXPOSE", page)
        with self.request("/admin/app.js") as response:
            script = response.read().decode()
        self.assertIn("renderCdnMonitor", script)
        self.assertNotIn("SECRET_TOKEN", script)
        self._status["multi_cdn"] = [{
            "selected_cabinet": 2,
            "switches": 1,
            "virtual_segment_sequence": 5,
            "cached_video_seconds": 24.0,
            "last_playlist_progress_seconds": 2.0,
            "switch_history": [{"at": 1795430000, "from_cabinet": 1,
                                "to_cabinet": 2, "reason": "PUBLISH_STALL",
                                "kind": "switch"}],
            "sources": [{"cabinet": 1, "fresh": False, "buffer_seconds": 0.0},
                        {"cabinet": 2, "fresh": True, "buffer_seconds": 22.0}]
        }]
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/admin/api/overview")
        self.assertEqual(denied.exception.code, 401)
        self.login().close()
        with self.request("/admin/api/overview") as response:
            data = json.load(response)
        self.assertEqual(data["multi_cdn"][0]["selected_cabinet"], 2)
        self.assertEqual(data["multi_cdn"][0]["switch_history"][0]["reason"], "PUBLISH_STALL")
        self.assertNotIn("PRIVATE_SECRET_NEVER_EXPOSE", json.dumps(data))

    def test_tv_links_are_available_only_to_logged_in_admin(self):
        token = "private-playlist-token-for-test"
        self.state.gateway_token_file.write_text(token, encoding="ascii")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/admin/api/tv-links")
        self.assertEqual(error.exception.code, 401)
        with self.request("/admin/") as response:
            self.assertNotIn(token, response.read().decode())
        with self.request("/admin/app.js") as response:
            self.assertNotIn(token, response.read().decode())
        self.login().close()
        with self.request("/admin/api/tv-links") as response:
            data = json.load(response)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertTrue(data["available"])
        self.assertEqual(data["m3u"],
                         "https://tv.heysmart.lv/" + token + "/channels.m3u")
        self.assertEqual(data["epg"],
                         "https://tv.heysmart.lv/" + token + "/epg.xml.gz")
        self.assertTrue(data["automatic_epg"])
        with self.request("/admin/api/logout", data=b"") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/admin/api/tv-links")
        self.assertEqual(error.exception.code, 401)

    def test_default_tv_token_is_bootstrapped_for_first_cabinet(self):
        self.login().close()
        with self.request("/admin/api/tv-links") as response:
            data = json.load(response)
        self.assertTrue(data["available"])
        self.assertIn("/channels.m3u", data["m3u"])
        with self.request("/admin/api/profiles") as response:
            profiles = json.load(response)["profiles"]
        self.assertEqual(len(profiles), 3)
        self.assertTrue(profiles[0]["configured"])
        self.assertFalse(profiles[1]["configured"])
        self.assertFalse(profiles[2]["configured"])
        self.assertEqual(profiles[0]["m3u"], data["m3u"])
        self.assertNotEqual(profiles[0]["m3u"], profiles[1]["m3u"])

    def test_new_private_history_and_incident_endpoints(self):
        # The two pages require the same independent admin session.
        for path in ("/admin/api/history?days=7", "/admin/api/incidents?days=7"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 401)
        store = Telemetry(self.state.telemetry_db)
        try:
            cid = admin.channels_from_text(PLAYLIST)[0].id
            timestamp = time.time() - 30
            store.observe_segment(cid, store.viewer("192.0.2.4", "LG"), timestamp)
            store.observe_segment(cid, store.viewer("192.0.2.4", "LG"), timestamp + 12)
            store.incident(cid, "stream_stalled", "UPSTREAM_NO_PROGRESS",
                           duration_seconds=31)
            store.flush()
            self.login().close()
            with self.request("/admin/api/history?days=7") as response:
                data = json.load(response)
            self.assertTrue(data["tracking_available"])
            self.assertEqual(data["total_sessions"], 1)
            self.assertEqual(data["total_seconds"], 12)
            self.assertEqual(data["channels"][0]["channel_name"], "Россия 24 HD")
            self.assertNotIn("192.0.2.4", json.dumps(data))
            with self.request("/admin/api/incidents?kind=stream_stalled&days=7") as response:
                incidents = json.load(response)
            self.assertEqual(incidents["total"], 1)
            self.assertEqual(incidents["events"][0]["channel_name"], "Россия 24 HD")
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/admin/api/history?days=500")
            self.assertEqual(error.exception.code, 400)
        finally:
            store.close()

    def test_export_all_incidents_requires_login_and_respects_filters(self):
        for route in ("/admin/api/incidents/export?days=7",
                      "/admin/api/incidents/clear"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route)
            self.assertEqual(error.exception.code, 401)
        store = Telemetry(self.state.telemetry_db)
        try:
            cid = admin.channels_from_text(PLAYLIST)[0].id
            for index in range(230):
                store.incident(cid, "segment_slow", "SEGMENT_DOWNLOAD_SLOW",
                               duration_seconds=11.4, timestamp=time.time()-index)
            store.incident(cid, "stream_stalled", "NO_DOWNLOADED_SEGMENTS")
            store.flush()
            self.login().close()
            with self.request("/admin/api/incidents/export?days=7") as response:
                content = response.read().decode("utf-8-sig")
                self.assertIn("text/csv", response.headers["Content-Type"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            rows = list(csv.DictReader(io.StringIO(content)))
            self.assertEqual(len(rows), 231)  # no 200-row UI limit
            self.assertIn("Россия 24 HD", rows[0]["channel"])
            self.assertNotIn("PRIVATE_SECRET_NEVER_EXPOSE", content)
            with self.request("/admin/api/incidents/export?days=7&kind=stream_stalled") as response:
                stalled = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
            self.assertEqual(len(stalled), 1)
            with self.assertRaises(urllib.error.HTTPError) as bad:
                self.request("/admin/api/incidents/export?days=999")
            self.assertEqual(bad.exception.code, 400)
        finally:
            store.close()

    def test_clear_incidents_only_after_confirmation_preserves_viewing_history(self):
        store = Telemetry(self.state.telemetry_db)
        try:
            cid = admin.channels_from_text(PLAYLIST)[0].id
            viewer = store.viewer("192.0.2.55", "Samsung")
            store.observe_segment(cid, viewer, time.time()-5)
            store.observe_segment(cid, viewer, time.time())
            store.incident(cid, "upstream_error", "MANIFEST_HTTP_404")
            store.incident(cid, "stream_stalled", "UPSTREAM_NO_PROGRESS")
            store.flush()
            self.login().close()
            url = self.url + "/admin/api/incidents/clear"
            valid_body = json.dumps({"confirm": "DELETE_INCIDENTS"}).encode()
            for headers, body in [
                ({"Content-Type": "application/json"}, valid_body),
                ({"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
                 b'{"confirm":"WRONG"}'),
            ]:
                with self.assertRaises(urllib.error.HTTPError) as response:
                    self.opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=4)
                self.assertIn(response.exception.code, (400, 403))
            with self.request("/admin/api/incidents?days=7") as response:
                self.assertEqual(json.load(response)["total"], 2)
            req = urllib.request.Request(
                url, data=valid_body,
                headers={"Content-Type": "application/json",
                         "X-Requested-With": "XMLHttpRequest"})
            with self.opener.open(req, timeout=4) as response:
                self.assertEqual(json.load(response)["deleted"], 2)
            with self.request("/admin/api/incidents?days=7") as response:
                self.assertEqual(json.load(response)["total"], 0)
            with self.request("/admin/api/history?days=7") as response:
                self.assertEqual(json.load(response)["total_sessions"], 1)
        finally:
            store.close()

    def test_profiles_api_renames_and_hides_provider_playlist_source(self):
        self.login().close()
        with self.request("/admin/api/profiles") as response:
            original = json.load(response)["profiles"]
        primary_url = original[0]["m3u"]
        payload = json.dumps({"name": "Родителям"}).encode()
        with self.request("/admin/api/profiles/1", data=payload, json_body=True) as response:
            rows = json.load(response)["profiles"]
        self.assertEqual(rows[0]["name"], "Родителям")
        self.assertEqual(rows[0]["m3u"], primary_url)
        self.assertEqual(rows[1]["name"], "Профиль 2")
        for row in rows:
            self.assertNotIn("source_url", row)
            self.assertNotIn("epg_url", row)
            self.assertNotIn("token", row)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.request("/admin/api/profiles/4", data=payload, json_body=True)
        self.assertEqual(err.exception.code, 404)

    def test_admin_can_save_remote_m3u_and_receives_private_playback_link(self):
        playlist = ('#EXTM3U\n'
                    '#EXTINF:-1 group-title="Тест",Другой канал\n'
                    'https://upstream.example/private/STREAM_CREDENTIAL\n')
        provider_url = "https://m3u.example/playlist?login=PRIVATE_123"
        body = json.dumps({"name": "ТВ родителей", "m3u_url": provider_url}).encode()
        with (patch("iptv_profiles.download_playlist", return_value=playlist),
              patch("iptv_profile_jobs.start_generation")):
            self.login().close()
            with self.request("/admin/api/profiles/2", data=body, json_body=True) as response:
                response_text = response.read().decode()
        data = json.loads(response_text)["profiles"]
        self.assertTrue(data[1]["configured"])
        self.assertEqual(data[1]["name"], "ТВ родителей")
        self.assertIn("/channels.m3u", data[1]["m3u"])
        self.assertNotEqual(data[1]["m3u"], data[0]["m3u"])
        self.assertEqual(data[1]["source_host"], "m3u.example")
        self.assertNotIn("PRIVATE_123", response_text)
        self.assertNotIn("STREAM_CREDENTIAL", response_text)
        self.assertNotIn("source_url", response_text)
        self.assertIn("Другой канал", self.state.profiles.snapshot_path(2).read_text())

    def test_profile_save_requires_login(self):
        payload = json.dumps({"name": "My TV"}).encode()
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request("/admin/api/profiles/2", data=payload, json_body=True)
        self.assertEqual(result.exception.code, 401)

    def test_logout_revokes_session(self):
        self.login().close()
        with self.request("/admin/api/logout", data=b"") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request("/admin/api/overview")
        self.assertEqual(result.exception.code, 401)

    def test_password_permissions(self):
        path = Path(self.tmp.name) / "admin-password"
        password = admin.read_or_create_password(path)
        self.assertGreaterEqual(len(password), 24)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            admin.read_or_create_password(path)

    def test_epg_scan_has_real_channel_coverage(self):
        result = admin.scan_epg(self.epg)
        first = admin.channels_from_text(PLAYLIST)[0]
        self.assertEqual(result["covered_ids"], {first.id})
        self.assertEqual(result["programmes"], 1)

    def test_methods_and_unknown_routes_are_restricted(self):
        with self.assertRaises(urllib.error.HTTPError) as response:
            self.request("/admin/unknown")
        self.assertEqual(response.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as response:
            self.request("/admin/api/overview", data=b"")
        self.assertEqual(response.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
