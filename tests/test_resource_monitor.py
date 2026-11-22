"""Read-only VPS resource sampler unit tests without reliance on real VPS."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iptv_resource_monitor as monitor


class ResourceMonitorTests(unittest.TestCase):
    def test_percent_delta_is_instantaneous_and_clamped(self):
        self.assertIsNone(monitor._percent_delta((300, 150), None))
        self.assertEqual(monitor._percent_delta((200, 100), (100, 80)), 80.0)
        self.assertEqual(monitor._percent_delta((100, 70), (100, 40)), None)
        self.assertEqual(monitor._percent_delta((200, 100), (100, 0)), 0.0)

    def test_reads_linux_meminfo_in_bytes(self):
        contents = "MemTotal:       1048576 kB\nMemAvailable:    262144 kB\n"
        with patch.object(monitor.Path, "read_text", return_value=contents):
            ram = monitor._memory()
        self.assertEqual(ram["total_bytes"], 1073741824)
        self.assertEqual(ram["used_bytes"], 805306368)
        self.assertEqual(ram["percent"], 75.0)

    def test_sample_uses_cpu_deltas_and_excludes_loopback_network(self):
        sampler = monitor.ResourceSampler(interval=0)
        fake_ram = {"used_bytes": 512, "total_bytes": 1024, "percent": 50.0}
        fake_disk = {"used_bytes": 100, "total_bytes": 1000, "percent": 10.0}
        with patch.object(monitor, "_cpu_ticks", side_effect=[(100, 30), (200, 60)]), \
             patch.object(monitor, "_memory", return_value=fake_ram), \
             patch.object(monitor, "_disk", return_value=fake_disk), \
             patch.object(monitor, "_network_bytes", side_effect=[(1000, 2000), (2000, 4000)]), \
             patch.object(monitor, "_gateway_pid", return_value=456), \
             patch.object(monitor, "_process_counters", side_effect=lambda pid:
                          (1, 300 if pid == 456 else 100, 17_000_000)):
            first = sampler.snapshot()
            self.assertIsNone(first["vps"]["cpu_percent"])
            self.assertIsNone(first["vps"]["rx_bytes_per_second"])
            self.assertIsNone(first["service"]["gateway"]["cpu_percent"])
            sampler.last_sample_at -= 2
            second = sampler.snapshot()
        self.assertEqual(second["vps"]["cpu_percent"], 70.0)
        self.assertEqual(second["vps"]["memory"]["percent"], 50.0)
        self.assertEqual(second["vps"]["disk"]["percent"], 10.0)
        self.assertGreaterEqual(second["vps"]["rx_bytes_per_second"], 0)
        self.assertEqual(second["service"]["gateway"]["rss_bytes"], 17_000_000)
        self.assertEqual(second["service"]["gateway"]["cpu_percent"], 0.0)
        self.assertNotIn("cmdline", str(second))
        self.assertNotIn("token", str(second))

    def test_process_sampler_survives_missing_gateway_systemd_unit(self):
        sampler = monitor.ResourceSampler(interval=0)
        with patch.object(monitor, "_cpu_ticks", side_effect=OSError()), \
             patch.object(monitor, "_memory", side_effect=OSError()), \
             patch.object(monitor, "_disk", side_effect=OSError()), \
             patch.object(monitor, "_network_bytes", side_effect=OSError()), \
             patch.object(monitor, "_gateway_pid", return_value=None), \
             patch.object(monitor, "_process_counters", side_effect=OSError()):
            sample = sampler.snapshot()
        self.assertIsNone(sample["vps"]["cpu_percent"])
        self.assertIsNone(sample["service"]["gateway"]["rss_bytes"])
        self.assertIsNone(sample["vps"]["memory"])

    def test_procmem_process_stats_indices(self):
        fields = ["S"] + ["0"] * 21
        fields[11] = "250"
        fields[12] = "100"
        fields[19] = "123456"
        fields[21] = "32"
        source = "4242 (iptv worker) " + " ".join(fields)
        with patch.object(monitor.Path, "read_text", return_value=source):
            start, cpu, rss = monitor._process_counters(4242)
        self.assertEqual((start, cpu), (123456, 350))
        self.assertEqual(rss, 32 * os.sysconf("SC_PAGE_SIZE"))

    def test_cpu_memory_stats_do_not_need_additional_packages(self):
        self.assertTrue(callable(monitor.ResourceSampler))
        self.assertEqual(monitor._percent_delta((200, 100), (100, 50)), 50.0)


if __name__ == "__main__":
    unittest.main()
