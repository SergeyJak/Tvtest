"""Cabinet, channel and session drill-down through the authenticated admin UI."""
import shutil
import time
import unittest
from unittest.mock import patch

import test_admin
from iptv_multi import channels_from_text
from iptv_telemetry import Telemetry

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "Rendered history test requires Playwright and Chromium")
class HistoryUiTests(unittest.TestCase):
    def test_stability_shows_delivery_per_cabinet_and_handles_lost_connection(self):
        fixture = test_admin.AdminTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._status["cabins"] = [{"slot": i, "telemetry_enabled": True} for i in (1, 2)]
        cid = channels_from_text(test_admin.PLAYLIST)[0].id
        with patch("iptv_profiles.download_playlist", return_value=test_admin.PLAYLIST):
            fixture.state.profiles.update(2, "Вадим", "https://provider.example/list")
        store = Telemetry(fixture.state.telemetry_db)
        self.addCleanup(store.close)
        ts = int(time.time()) - 10
        for key in (cid, "p2:" + cid):
            store.observe_segment(key, "anonymous", ts, piece="v.2", source=2)
        for offset in (1, 2):
            store.incident("p2:" + cid, "client_error", "HTTP_404_SEGMENT_CACHE_MISS",
                           timestamp=ts+offset, diagnostics={"client":"anonymous"})
        store.incident(cid, "upstream_error", "MANIFEST_HTTP_404", timestamp=ts+2)
        store.flush()
        with sync_playwright() as playwright:
            chrome = shutil.which("chromium") or shutil.which("google-chrome")
            browser = playwright.chromium.launch(executable_path=chrome)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(fixture.url + "/admin/")
            page.locator("#password").fill(fixture.pwd)
            page.locator("#login-button").click()
            page.locator('[data-target="stability"]').click()
            live = page.locator("#stability-live")
            expect(live).to_contain_text("Видео через резерв")
            expect(live).to_contain_text("Проблема с выдачей")
            expect(page.locator("#stability-total")).to_contain_text("Ошибок: 2 · Эпизодов: 1")
            page.locator("#stability-slot").select_option("1")
            expect(page.locator("#stability-total")).to_contain_text("Ошибок: 0 · Эпизодов: 0")
            expect(live).not_to_contain_text("Проблема с выдачей")
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            page.route("**/admin/api/stability?**", lambda route: route.fulfill(status=503, body="{}"))
            page.locator("#stability-days").select_option("7")
            expect(page.locator("#stability-notice")).to_contain_text("Не удалось")
            expect(live).to_be_empty()
            browser.close()

    def test_history_selection_and_event_pagination(self):
        fixture = test_admin.AdminTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with patch("iptv_profiles.download_playlist", return_value=test_admin.PLAYLIST):
            fixture.state.profiles.update(2, "Вадим", "https://provider.example/list")
        cid = channels_from_text(test_admin.PLAYLIST)[0].id
        store = Telemetry(fixture.state.telemetry_db)
        self.addCleanup(store.close)
        ts = int(time.time()) - 500
        for key in (cid, "p2:" + cid):
            for offset in (0, 60, 200, 210):
                store.observe_segment(key, "device", ts + offset)
        store.incident(cid, "stream_stalled", "PRIMARY_ONLY", timestamp=ts)
        for offset in range(51):
            store.incident("p2:" + cid, "segment_slow", "SLOW_" + str(offset), timestamp=ts + offset)
        store.incident("p2:" + cid, "stream_recovered", "RECOVERED", timestamp=ts + 205)
        store.flush()
        with sync_playwright() as playwright:
            chrome = shutil.which("chromium") or shutil.which("google-chrome")
            browser = playwright.chromium.launch(executable_path=chrome)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(fixture.url + "/admin/")
            page.locator("#password").fill(fixture.pwd)
            page.locator("#login-button").click()
            page.locator('[data-target="history"]').click()
            ranking = page.locator("#history-ranking")
            expect(ranking).to_contain_text("События · 1")
            page.locator("#history-slot").select_option("2")
            expect(ranking).to_contain_text("События · 52")
            ranking.get_by_role("button").click()
            expect(page.locator(".history-event-row")).to_have_count(50)
            page.locator("#history-event-next").click()
            expect(page.locator(".history-event-row")).to_have_count(2)
            expect(page.locator("#history-event-list")).not_to_contain_text("PRIMARY_ONLY")
            page.locator("#history-recent button").first.click()
            expect(page.locator(".history-event-row")).to_have_count(1)
            expect(page.locator("#history-event-list")).to_contain_text("RECOVERED")
            expect(page.locator("#history-event-title")).to_contain_text("Вадим · Россия 24 HD")
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            page.locator("#history-slot").select_option("0")
            expect(page.locator("#history-events")).to_be_hidden()
            expect(page.locator(".rank-row")).to_have_count(2)
            self.assertEqual(errors, [])
            browser.close()
