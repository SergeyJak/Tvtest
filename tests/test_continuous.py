import unittest
import urllib.error
from unittest.mock import patch

import iptv_continuous as c


class ContinuousTests(unittest.TestCase):
    def test_media_sequence_and_relative_segment(self):
        body = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:42\n'
                '#EXTINF:5.5,\nvideo/42.ts\n#EXTINF:6.0,\nvideo/43.ts\n')
        m = c.parse_media(body, 'https://example.com/hls/index.m3u8?token=abc')
        self.assertEqual([s.sequence for s in m.segments], [42, 43])
        self.assertEqual(m.target_duration, 6)
        self.assertEqual(m.segments[0].url, 'https://example.com/hls/video/42.ts')

    def test_reject_master_as_media(self):
        with self.assertRaises(ValueError):
            c.parse_media('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n',
                          'https://example.com/index.m3u8')

    def test_error_contains_no_credentials(self):
        error = urllib.error.URLError('secret-token-xyz')
        self.assertEqual(c.safe_status(error), 'NETWORK_ERROR')
        self.assertEqual(c.safe_status(
            urllib.error.HTTPError('http://secret-token', 403, 'x', None, None)), 'HTTP_403')

    def test_spreadsheet_formula_escape(self):
        self.assertEqual(c.row('=SECRET', 'OK')['channel'], "'=SECRET")

    def test_fast_segment_does_not_consume_old_history(self):
        m = c.parse_media('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-TARGETDURATION:5\n'
                          '#EXTINF:5,\n7.ts\n#EXTINF:5,\n8.ts\n', 'https://example.com/index.m3u8')
        seen = []
        with patch.object(c, 'fetch_media', return_value=(m, 10)), \
             patch.object(c, 'download_segment', return_value=('OK', 100, 10, 20)), \
             patch.object(c.time, 'sleep', side_effect=lambda _: None):
            c.observe(('test', '', 'https://example.com/index.m3u8'),
                      0.001, 0.01, 2, 4096, seen.append)
        segments = [x for x in seen if x['status'] == 'OK']
        self.assertEqual([x['sequence'] for x in segments], [8])

    def test_stale_playlist_is_detected_once(self):
        m = c.parse_media('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-TARGETDURATION:5\n'
                          '#EXTINF:5,\n7.ts\n', 'https://example.com/index.m3u8')
        seen = []
        clock = [0.0]
        def advance(n):
            clock[0] += n
        with patch.object(c, 'fetch_media', return_value=(m, 10)), \
             patch.object(c, 'download_segment', return_value=('OK', 100, 10, 20)), \
             patch.object(c.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(c.time, 'sleep', side_effect=advance):
            c.observe(('test', '', 'https://example.com/index.m3u8'),
                      0.5, 2, 2, 4096, seen.append)
        self.assertEqual(sum(x['status'] == 'STALE_MANIFEST' for x in seen), 1)

    def test_slow_segment_detection(self):
        m = c.parse_media('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n#EXT-X-TARGETDURATION:5\n'
                          '#EXTINF:5,\n5.ts\n', 'https://example.com/index.m3u8')
        seen = []
        with patch.object(c, 'fetch_media', return_value=(m, 10)), \
             patch.object(c, 'download_segment', return_value=('OK', 100, 10, 6000)), \
             patch.object(c.time, 'sleep', side_effect=lambda _: None):
            c.observe(('test', '', 'https://example.com/index.m3u8'),
                      0.001, 0.01, 2, 4096, seen.append)
        self.assertEqual(seen[0]['status'], 'SLOW_SEGMENT')


if __name__ == '__main__':
    unittest.main()
