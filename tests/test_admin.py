"""Offline integration tests for isolated HeySmart TV admin service."""
import gzip
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
