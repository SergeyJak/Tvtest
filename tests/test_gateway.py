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

    def test_safe_errors_do_not_expose_access_key(self):
        error = urllib.error.URLError('http://secret.example/key/ABCDEF')
        self.assertEqual(g.safe_error(error), 'NETWORK_ERROR')
        self.assertEqual(g.safe_error(ValueError('secret-token-in-error')), 'INVALID_HLS')


if __name__ == '__main__':
    unittest.main()
