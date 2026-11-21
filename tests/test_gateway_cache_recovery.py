"""Regression tests for TV freezes despite a healthy HLS upstream."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import iptv_gateway as g


class GatewayRecoveryTests(unittest.TestCase):
    @staticmethod
    def gateway(sequences, delay=90):
        gw = g.Gateway('http://upstream.example.test/live', delay=delay)
        for seq in sequences:
            gw.cache[seq] = (10.0, b'x' * 16)
            gw.total_bytes += 16
        gw.latest = max(sequences)
        return gw

    def test_first_start_waits_for_real_delay(self):
        gw = self.gateway(range(100, 106))
        self.assertEqual(gw.published(), [])
        result = json.loads(gw.status())
        self.assertFalse(result['ready'])
        self.assertEqual(result['contiguous_tail_seconds'], 60.0)

    def test_after_stall_gap_rebuilds_without_manifest_503(self):
        gw = self.gateway(range(100, 120))
        initial = gw.published()
        self.assertGreaterEqual(len(initial), 3)
        previous_seq = initial[-1][0]
        # Segment 116 never arrived. New live fragments are present, but their
        # run is too short to fill 90s and old code returned HTTP 503 forever.
        del gw.cache[116]
        for seq in range(120, 123):
            gw.cache[seq] = (10.0, b'x' * 16)
            gw.total_bytes += 16
        gw.latest = 122
        result = json.loads(gw.status())
        self.assertTrue(result['ready'])
        self.assertEqual(result['contiguous_tail_segments'], 6)
        self.assertEqual(result['contiguous_tail_seconds'], 60)
        self.assertLess(result['effective_buffer_delay_seconds'], 90)
        playlist = gw.playlist('lan-token').decode('utf-8')
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:', playlist)
        self.assertGreaterEqual(gw.last_published_seq, previous_seq)

    def test_multiple_playlists_keep_advancing_after_missing_segment(self):
        gw = self.gateway(range(100, 120), delay=90)
        clock = [100.0]
        with patch.object(g.time, 'monotonic', side_effect=lambda: clock[0]):
            before = gw.published()
            self.assertEqual(before[-1][0], 110)
            # Missing 116 splits cache into old 100..115 and new 117..119.
            del gw.cache[116]
            self.assertEqual(gw.published()[-1][0], 110)
            # Preserve old cached window at the advertised 10s video pace,
            # even while the newer run is too small for the 90s buffer.
            for seq in range(111, 116):
                clock[0] += 10
                records = gw.published()
                self.assertTrue(records, 'must not emit HTTP 503 while old cache remains')
                self.assertEqual(records[-1][0], seq)

            # Cross the gap and start with the new contiguous run.
            clock[0] += 10
            self.assertEqual(gw.published()[-1][0], 119)
            self.assertTrue(gw._recovery_mode)

            ends = []
            for seq in range(120, 134):
                gw.cache[seq] = (10.0, b'x' * 16)
                gw.latest = seq
                clock[0] += 10
                playlist = gw.playlist('test-token')
                self.assertIsNotNone(playlist)
                ends.append(gw.last_published_seq)
                self.assertEqual(gw.published(commit=False)[-1][0], ends[-1])
            self.assertGreater(ends[-1], 130)
            self.assertEqual(ends, sorted(ends))
            self.assertGreaterEqual(len(set(ends)), 11,
                                    'published edge must not freeze until full delay fills')

    def test_status_reports_age_of_published_window_without_moving_cursor(self):
        gw = self.gateway(range(100, 120), delay=90)
        clock = [100.0]
        with patch.object(g.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertIsNone(json.loads(gw.status())['seconds_since_published_advance'])
            start = gw.published()[-1][0]
            clock[0] += 25
            info = json.loads(gw.status())
            self.assertEqual(info['last_published_sequence'], start)
            self.assertEqual(info['seconds_since_published_advance'], 25.0)
            self.assertFalse(info['recovery_mode'])
            # Polling /status cannot advance HLS cursor.
            self.assertEqual(gw.last_published_seq, start)

    def test_status_is_read_only_for_tv_cursor(self):
        gw = self.gateway(range(100, 120))
        self.assertIsNone(gw.last_published_seq)
        for _ in range(3):
            self.assertTrue(json.loads(gw.status())['ready'])
        self.assertIsNone(gw.last_published_seq)
        gw.playlist('lan-token')
        self.assertIsNotNone(gw.last_published_seq)

    def test_does_not_rewind_sequence_after_old_cache_tail(self):
        gw = self.gateway(range(100, 109))
        gw.last_published_seq = 120
        self.assertFalse(json.loads(gw.status())['ready'])
        self.assertEqual(gw.published(), [])

    def test_cache_diagnostics_do_not_expose_upstream_url(self):
        secret = 'SECRET_STREAM_TOKEN'
        gw = g.Gateway(f'http://upstream.example.test/{secret}', delay=90)
        for seq in range(10, 30):
            gw.cache[seq] = (10.0, b'x')
        gw.latest = 29
        info = gw.status().decode('utf-8')
        self.assertNotIn(secret, info)
        self.assertIn('contiguous_tail_segments', info)
        self.assertIn('seconds_since_segment_download', info)

    def test_client_buffering_response_is_counted(self):
        gw = self.gateway(range(100, 103))  # Not enough for 90s startup.
        server = ThreadingHTTPServer(('127.0.0.1', 0),
                                     g.make_handler(gw, 'lan-token', 'Test'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root = f'http://127.0.0.1:{server.server_port}/lan-token'
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(root + '/index.m3u8', timeout=4)
            self.assertEqual(ctx.exception.code, 503)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(root + '/segment/1.ts', timeout=4)
            self.assertEqual(ctx.exception.code, 404)
            info = json.loads(gw.status())
            self.assertEqual(info['client_manifest_503'], 1)
            self.assertEqual(info['client_segment_404'], 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
