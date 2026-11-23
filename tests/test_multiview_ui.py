"""Rendered multi-cabinet dashboard regression, using the local HTTP gateway fixtures."""
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import shutil
import threading
import unittest

import test_profiles
from iptv_admin import make_admin_handler

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "Rendered UI test requires Playwright and Chromium")
class MultiViewUiTests(unittest.TestCase):
    def test_concurrent_viewers_keep_their_channel_and_recover_from_a_failed_poll(self):
        fixture = test_profiles.ThreeCabinetTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        tokens = [fixture.store.get(1)["token"], fixture.configure_backup(2), fixture.configure_backup(3)]
        first, second = fixture.base_manager.order
        requests = list(zip(tokens, (first, first, second)))
        for token, cid in requests:
            worker = fixture.registry.resolve(token)[0].start(cid)
            fixture.fill_cache(worker, "local-video")

        def watch(request):
            token, cid = request
            with fixture.request(token, f"channel/{cid}/index.m3u8") as response:
                return response.status

        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(list(pool.map(watch, requests)), [200, 200, 200])

        state = fixture.admin_state()
        state.password = "local-ui-test-password-for-login"
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_admin_handler(state, secure_cookies=False))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        with sync_playwright() as playwright:
            chrome = shutil.which("chromium") or shutil.which("google-chrome")
            browser = playwright.chromium.launch(executable_path=chrome)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/admin/")
            page.locator("#password").fill(state.password)
            page.locator("#login-button").click()
            cards = page.locator("#cdn-sources button")
            expect(cards).to_have_count(3)
            for slot, channel in enumerate(("Channel A", "Channel A", "Channel B"), start=1):
                card = page.locator(f"#cdn-sources button[data-home='{slot}']")
                expect(card).to_contain_text(channel)
            cards.nth(2).click()
            expect(page.locator("#cdn-channel-help")).to_contain_text("Сейчас смотрят: Channel B")
            expect(page.locator("#cdn-monitor-selected")).to_have_text("TV 3")
            page.locator("#cdn-cabin-switch [data-slot='2']").click()
            expect(page.locator("#cdn-channel-help")).to_contain_text("Сейчас смотрят: Channel A")
            expect(page.locator("#cdn-monitor-selected")).to_have_text("TV 2")

            endpoint = "**/admin/api/overview"
            page.route(endpoint, lambda route: route.fulfill(status=503, json={}))
            page.locator("#refresh-button").click()
            expect(page.locator("#connection-error")).to_be_visible()
            expect(page.locator("#cdn-monitor-status")).to_contain_text("Нет связи")
            expect(page.locator("#cdn-monitor-selected")).to_have_text("—")
            expect(page.locator(".cdn-cabin-online")).to_have_count(0)
            page.unroute(endpoint)
            page.locator("#refresh-button").click()
            expect(page.locator("#connection-error")).to_be_hidden()
            expect(page.locator("#cdn-monitor-selected")).to_have_text("TV 2")
            expect(page.locator(".cdn-cabin-online")).to_have_count(3)
            self.assertEqual(errors, [])
            browser.close()
