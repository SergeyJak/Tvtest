import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import iptv_gateway as g


class GatewayTests(unittest.TestCase):
    def test_parse_media_and_unsupported_features(self):
        text = ('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:45\n#EXT-X-TARGETDURATION:10\n'
                '#EXTINF:9.8,\n45.ts\n#EXTINF:9.8,\n46.ts\n')
        target, segments = g.parse_media(text, 'http://example.test/hls/main.m3u8')
        self.assertEqual(target, 10)
        self.assertEqual([s.sequence for s in segments], [45, 46])
        self.assertEqual(segments[0].url, 'http://example.test/hls/45.ts')
        with self.assertRaisesRegex(ValueError, 'unsupported_hls_features'):
            g.parse_media(text + '#EXT-X-KEY:METHOD=AES-128,URI="key"\n', 'http://example.test/')

    def test_delay_enforced_and_contiguous_tail(self):
        gateway = g.Gateway('http://upstream.test', delay=40)
        self.assertEqual(gateway.published(), [])
        for seq in range(100, 112):
            gateway.cache[seq] = (10, bytes([seq % 256]))
        gateway.latest = 111
        records = gateway.published()
        self.assertEqual(records[-1][0], 107)  # 4 newer segments kept ahead
        self.assertGreaterEqual(len(records), 3)
        manifest = gateway.playlist('test-token').decode()
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:100', manifest)
        self.assertIn('/test-token/segment/107.ts', manifest)
        self.assertNotIn('/test-token/segment/108.ts', manifest)
        del gateway.cache[104]
        records = gateway.published()
        self.assertEqual([x[0] for x in records], [105, 106, 107])

    def test_buffer_releases_cached_segments_during_stall_without_rewind(self):
        gateway = g.Gateway('http://upstream.test', delay=40)
        for seq in range(100, 112):
            gateway.cache[seq] = (10, b'packet')
        gateway.latest = 111
        with patch.object(g.time, 'monotonic', return_value=100.0):
            initial = gateway.published()[-1][0]
        with patch.object(g.time, 'monotonic', return_value=120.0):
            during_stall = gateway.published()[-1][0]
        self.assertGreater(during_stall, initial)
        gateway.cache[112] = (10, b'packet')
        gateway.latest = 112
        with patch.object(g.time, 'monotonic', return_value=121.0):
            on_recovery = gateway.published()[-1][0]
        self.assertGreaterEqual(on_recovery, during_stall)

    def test_gateway_download_caches_segments(self):
        manifest = ('#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:5\n'
                    '#EXTINF:10,\n5.ts\n#EXTINF:10,\n6.ts\n')
        gateway = g.Gateway('http://secret.example/secret', delay=30)
        with patch.object(g, 'media_manifest', return_value=(manifest, 'http://secret.example/hls/index.m3u8')), \
             patch.object(g, 'fetch_segment', return_value=b'\x47DATA') as download:
            gateway.cycle()
            gateway.cycle()
        self.assertEqual(download.call_count, 2)
        self.assertEqual(gateway.segment(5), b'\x47DATA')
        self.assertEqual(gateway.last_error, None)
        self.assertEqual(gateway.total_bytes, 10)

    def test_upstream_stale_status_and_out_of_order_cache(self):
        gateway = g.Gateway('http://upstream.test', delay=40)
        gateway.cache[101] = (10, b'b')
        gateway.cache[100] = (10, b'a')
        gateway.cache = g.OrderedDict(sorted(gateway.cache.items()))
        self.assertEqual(list(gateway.cache), [100, 101])
        gateway.last_progress = g.time.time() - 61
        self.assertTrue(json.loads(gateway.status())['upstream_stalled'])

    def test_http_playlist_and_range_and_auth(self):
        gateway = g.Gateway('http://example.test', delay=30)
        for seq in range(30, 45):
            gateway.cache[seq] = (10, b'0123456789')
        gateway.latest = 44
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), g.make_handler(gateway, 'random-token', 'VF Test'))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{httpd.server_port}'
        try:
            with urllib.request.urlopen(root + '/random-token/channels.m3u') as response:
                playlist = response.read().decode()
            self.assertIn('/random-token/index.m3u8', playlist)
            with urllib.request.urlopen(root + '/random-token/index.m3u8') as response:
                manifest = response.read().decode()
            self.assertIn('#EXT-X-MEDIA-SEQUENCE:', manifest)
            with urllib.request.urlopen(root + '/random-token/status') as response:
                status = json.loads(response.read())
            self.assertTrue(status['ready'])
            req = urllib.request.Request(root + '/random-token/segment/40.ts', headers={'Range': 'bytes=2-5'})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b'2345')
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(root + '/not-the-token/index.m3u8')
            self.assertEqual(ctx.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_upstream_failures_survive_next_successful_manifest_without_secrets(self):
        secret = 'PRIVATE_TOKEN_SHOULD_NOT_LEAK'
        gw = g.Gateway(f'http://example.test/{secret}', delay=30)
        manifest = ('#EXTM3U\n#EXT-X-TARGETDURATION:10\n'
                    '#EXT-X-MEDIA-SEQUENCE:5\n#EXTINF:10,\n5.ts\n')
        # Simulate a failed video fetch, then successful retry. Merely
        # downloading a manifest must not erase the failure history.
        failure = urllib.error.HTTPError(f'http://example.test/{secret}',
                                         404, secret, None, None)
        with patch.object(g, 'media_manifest',
                          return_value=(manifest, 'http://example.test/')), \
             patch.object(g, 'fetch_segment',
                          side_effect=[failure, b'\x47complete']):
            gw.cycle()
            self.assertEqual(gw.errors, 1)
            self.assertEqual(gw.last_error, 'SEGMENT_HTTP_404')
            gw.cycle()
        status = json.loads(gw.status())
        self.assertIsNone(status['last_error'])
        self.assertEqual(status['last_upstream_failure'], 'SEGMENT_HTTP_404')
        self.assertEqual(status['error_breakdown']['SEGMENT_HTTP_404'], 1)
        self.assertEqual(status['recent_upstream_failures'][-1]['sequence'], 5)
        self.assertIsNotNone(status['seconds_since_last_upstream_failure'])
        self.assertNotIn(secret, json.dumps(status))

    def test_upstream_failure_history_is_bounded_and_counts_types(self):
        gw = g.Gateway('http://upstream.test')
        for _ in range(15):
            gw.record_upstream_failure('SEGMENT', TimeoutError('secret'))
        gw.record_upstream_failure('MANIFEST', ValueError('secret_url'))
        status = json.loads(gw.status())
        self.assertEqual(status['errors'], 16)
        self.assertEqual(status['error_breakdown']['SEGMENT_TIMEOUT'], 15)
        self.assertEqual(status['error_breakdown']['MANIFEST_INVALID_HLS'], 1)
        self.assertEqual(len(status['recent_upstream_failures']), 8)
        self.assertNotIn('secret', json.dumps(status))
        self.assertEqual(status['last_upstream_failure'], 'MANIFEST_INVALID_HLS')

    def test_manifest_404_is_backed_off_and_refreshes_provider_playlist(self):
        secret = "https://provider.example/hidden/SECRET"
        failure = urllib.error.HTTPError(secret, 404, "gone", None, None)
        gw = g.Gateway(secret, poll=2, source_refresher=lambda: secret)
        clock = [100.0]
        with patch.object(g, "media_manifest", side_effect=failure) as fetch, \
             patch.object(g.time, "monotonic", side_effect=lambda: clock[0]):
            gw.cycle()
            clock[0] = 101.0
            gw.cycle()
            clock[0] = 102.0
            gw.cycle()  # no new HTTP request during backoff
            clock[0] = 107.0
            gw.cycle()
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(gw.manifest_failure_streak, 3)
        self.assertEqual(gw.consecutive_manifest_404, 3)
        self.assertEqual(gw.source_refresh_attempts, 1)
        self.assertNotIn("SECRET", json.dumps(json.loads(gw.status())))

    def test_live_manifest_failures_never_wait_longer_than_six_seconds(self):
        secret = "https://provider.example/private/ACCESSKEY"
        failure = urllib.error.HTTPError(secret, 404, "expired", None, None)
        gw = g.Gateway(secret, poll=2, source_refresher=lambda: secret)
        clock = [100.0]
        with patch.object(g, "media_manifest", side_effect=failure) as fetch, \
             patch.object(g.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in range(12):
                gw.cycle()
                self.assertLessEqual(gw.manifest_backoff_until - clock[0], 6.0)
                clock[0] = max(clock[0] + 2, gw.manifest_backoff_until)
        self.assertGreaterEqual(fetch.call_count, 5)
        self.assertNotIn("ACCESSKEY", json.dumps(json.loads(gw.status())))

    def test_new_source_url_invalidates_overlapping_old_video_cache(self):
        old_url = "https://source.example/old/SECRET"
        new_url = "https://source.example/new/SECRET"
        gw = g.Gateway(old_url, source_refresher=lambda: new_url, delay=30)
        gw.cache[55] = (6, b"old-video")
        gw.total_bytes = len(b"old-video")
        gw.latest = 55
        gw.last_published_seq = 55
        manifest = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
                    '#EXT-X-MEDIA-SEQUENCE:55\n'
                    '#EXTINF:6,\n55.ts\n')
        failure = urllib.error.HTTPError(old_url, 404, "expired", None, None)
        with patch.object(g, "media_manifest", side_effect=[
            failure, (manifest, new_url)]), \
             patch.object(g, "fetch_segment", return_value=b"fresh-video") as download:
            gw.cycle()
            self.assertEqual(gw.segment(55), b"old-video")
            self.assertEqual(gw._url, new_url)
            gw.cycle()
        self.assertEqual(download.call_count, 1)
        self.assertEqual(gw.segment(55), b"fresh-video")
        self.assertIsNone(gw.last_published_seq)
        self.assertEqual(gw.source_changes, 1)
        self.assertEqual(gw.manifest_failure_streak, 0)

    def test_consecutive_manifest_502_rotates_source_and_resets_backoff(self):
        old = "https://source.example/old"
        new = "https://source.example/new"
        gw = g.Gateway(old, source_refresher=lambda: new)
        failure = urllib.error.HTTPError(old, 502, "bad gateway", None, None)
        with patch.object(g, "media_manifest", side_effect=failure):
            gw.cycle()
            gw.cycle()
        self.assertEqual(gw.source_changes, 1)
        self.assertEqual(gw._url, new)
        self.assertEqual(gw.manifest_failure_streak, 0)
        self.assertEqual(gw.manifest_backoff_until, 0)

    def test_repeated_segment_502_refreshes_source_and_resumes(self):
        old = "https://provider.example/old-secret/stream.m3u8"
        new = "https://provider.example/new-secret/stream.m3u8"
        gw = g.Gateway(old, source_refresher=lambda: new, delay=30)
        manifest = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
                    '#EXT-X-MEDIA-SEQUENCE:10\n'
                    '#EXTINF:6,\n10.ts\n#EXTINF:6,\n11.ts\n'
                    '#EXTINF:6,\n12.ts\n')
        failure = urllib.error.HTTPError(old, 502, "bad gateway", None, None)
        with patch.object(g, "media_manifest", side_effect=[
            (manifest, old), (manifest, new) ]), \
             patch.object(g, "fetch_segment", side_effect=[
                 failure, failure, b"new10", b"new11", b"new12"]) as download:
            gw.cycle()
            self.assertEqual(gw._url, new)
            self.assertEqual(gw.source_changes, 1)
            gw.cycle()
        self.assertEqual(download.call_count, 5)
        self.assertEqual(gw.segment(10), b"new10")
        self.assertEqual(gw.segment_failure_streak, 0)
        self.assertNotIn("secret", json.dumps(json.loads(gw.status())))

    def test_sliding_window_with_expired_prefix_still_publishes_live_segments(self):
        base = "https://provider.example/live.m3u8"
        gw = g.Gateway(base, delay=0, source_refresher=lambda: base)
        for first in range(100, 106):
            manifest = (f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{first}\n" +
                        "".join(f"#EXTINF:6,\n{i}.ts\n" for i in range(first, first + 6)))

            def download(url, timeout, max_bytes):
                sequence = int(url.rsplit("/", 1)[1].split(".")[0])
                if sequence < first + 3:
                    raise urllib.error.HTTPError(url, 404, "expired", None, None)
                return b"fresh"

            with patch.object(g, "media_manifest", return_value=(manifest, base)), \
                 patch.object(g, "fetch_segment", side_effect=download):
                gw.cycle()
            self.assertEqual(gw.published()[-1][0], first + 5)

    def test_fully_expired_window_limits_downloads_per_poll(self):
        base = "https://provider.example/live.m3u8"
        gw = g.Gateway(base)
        manifest = "#EXTM3U\n" + "".join(f"#EXTINF:6,\n{i}.ts\n" for i in range(20))
        failure = urllib.error.HTTPError(base, 404, "expired", None, None)
        with patch.object(g, "media_manifest", return_value=(manifest, base)), \
             patch.object(g, "fetch_segment", side_effect=failure) as download:
            for _ in range(3):
                download.reset_mock()
                gw.cycle()
                self.assertEqual(download.call_count, 3)

    def test_safe_errors_do_not_expose_access_key(self):
        error = urllib.error.URLError('http://secret.example/key/ABCDEF')
        self.assertEqual(g.safe_error(error), 'NETWORK_ERROR')
        self.assertEqual(g.safe_error(ValueError('secret-token-in-error')), 'INVALID_HLS')


if __name__ == '__main__':
    unittest.main()
