import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iptv_gateway as g


class TestStableGatewayLink(unittest.TestCase):
    def test_generated_token_persists_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / ".iptv_gateway_token"
            first = g.load_or_create_token(token_path)
            second = g.load_or_create_token(token_path)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 22)
            self.assertEqual(token_path.read_text(encoding="ascii").strip(), first)

    def test_migrate_existing_tv_url_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / ".iptv_gateway_token"
            previous_token = "anExistingOldToken12345"
            token_path.write_text(previous_token, encoding="ascii")
            self.assertEqual(g.load_or_create_token(token_path), previous_token)

    def test_invalid_existing_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / ".iptv_gateway_token"
            for value in ("/path/to/tv", "http://example.com/private",
                          "x", "token with spaces", "0" * 130):
                with self.subTest(value=value[:10]):
                    token_path.write_text(value, encoding="ascii")
                    with self.assertRaisesRegex(ValueError, "invalid_token_file"):
                        g.load_or_create_token(token_path)

    def test_existing_token_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / ".iptv_gateway_token"
            existing = "ExistingTokenForTV2222"
            token_path.write_text(existing, encoding="ascii")
            with patch.object(g.secrets, "token_urlsafe", side_effect=AssertionError("overwritten")):
                self.assertEqual(g.load_or_create_token(token_path), existing)


if __name__ == "__main__":
    unittest.main()
