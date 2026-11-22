"""Check the safe systemd telemetry installer without touching systemd."""
import unittest
from deploy.enable_telemetry import add_option


class TelemetryDeploymentTests(unittest.TestCase):
    def test_single_line_preserves_existing_arguments(self):
        unit = """[Service]
ExecStart=/usr/bin/python3 iptv_multi.py --epg-file /tmp/epg.xml.gz --public-base-url https://tv.example.net
Restart=on-failure
"""
        result = add_option(unit, "--telemetry-db /private/usage.sqlite3")
        self.assertIn("--epg-file /tmp/epg.xml.gz", result)
        self.assertIn("--public-base-url https://tv.example.net", result)
        self.assertIn("https://tv.example.net --telemetry-db /private/usage.sqlite3", result)
        self.assertIn("Restart=on-failure", result)
        self.assertEqual(add_option(result, "--telemetry-db /private/usage.sqlite3"), result)

    def test_multiline_execstart_keeps_continuations(self):
        unit = ("[Service]\nExecStart=/usr/bin/python3 iptv_multi.py \\\n"
                "    --public-base-url https://tv.example.net \\\n"
                "    --epg-file /tmp/epg.xml.gz\n")
        result = add_option(unit, "--telemetry-db /private/usage.sqlite3")
        self.assertIn("    --epg-file /tmp/epg.xml.gz --telemetry-db", result)
        self.assertEqual(result.count("ExecStart="), 1)

    def test_ambiguous_or_unexpected_unit_rejected(self):
        with self.assertRaises(ValueError):
            add_option("[Service]\nExecStart=/usr/bin/other\n",
                       "--telemetry-db /tmp/db")
        with self.assertRaises(ValueError):
            add_option("[Service]\nExecStart=/usr/bin/python3 iptv_multi.py\n"
                       "ExecStart=/usr/bin/python3 iptv_multi.py\n",
                       "--telemetry-db /tmp/db")


if __name__ == "__main__":
    unittest.main()
