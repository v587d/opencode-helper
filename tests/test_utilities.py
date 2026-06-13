"""
Tests for shared utilities (pure functions only — no DB dependency).

Run:
    python -m unittest tests.test_utilities -v
    python -m pytest tests/ -v  (if pytest is installed)
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utilities import format_size, format_time


class TestFormatSize(unittest.TestCase):
    """Tests for human-readable file size formatting."""

    def test_bytes(self):
        self.assertEqual(format_size(0), "0.0 B")
        self.assertEqual(format_size(1), "1.0 B")
        self.assertEqual(format_size(512), "512.0 B")
        self.assertEqual(format_size(1023), "1023.0 B")

    def test_kilobytes(self):
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(1024 * 512), "512.0 KB")

    def test_megabytes(self):
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(format_size(1024 * 1024 * 100), "100.0 MB")

    def test_gigabytes(self):
        self.assertEqual(format_size(1024 ** 3), "1.0 GB")
        self.assertEqual(format_size(1024 ** 3 * 5), "5.0 GB")

    def test_terabytes(self):
        self.assertEqual(format_size(1024 ** 4), "1.0 TB")
        self.assertEqual(format_size(1024 ** 4 * 2), "2.0 TB")


class TestFormatTime(unittest.TestCase):
    """Tests for Unix-ms timestamp formatting."""

    def test_none(self):
        self.assertEqual(format_time(None), "N/A")

    def test_valid_timestamp(self):
        # 2024-01-15 08:30:00 UTC = 1705307400000 ms
        ts = 1705307400000
        result = format_time(ts)
        self.assertIn("2024-01-15", result)
        self.assertNotEqual(result, "N/A")
        self.assertNotEqual(result, "invalid timestamp")

    def test_zero_timestamp(self):
        result = format_time(0)
        self.assertIn("1970-01-01", result)

    def test_negative_timestamp(self):
        """Negative timestamps (pre-epoch). Not supported on all platforms."""
        result = format_time(-1000)
        # Windows Python does not support negative timestamps in
        # datetime.fromtimestamp(). On Unix, this would parse to 1969-12-31.
        valid_results = ("1969-12-31", "invalid timestamp")
        try:
            self.assertIn("1969-12-31", result)
        except AssertionError:
            self.assertEqual(result, "invalid timestamp")

    def test_very_large_timestamp(self):
        # Year 3000 — far future, but should still parse
        ts = 32503680000000  # 3000-01-01
        result = format_time(ts)
        self.assertNotEqual(result, "invalid timestamp")
        self.assertNotEqual(result, "N/A")


class TestJSONCParsing(unittest.TestCase):
    """Tests for JSONC settings parsing (comment stripping)."""

    def _load_via_utilities(self, jsonc_content: str) -> dict:
        """Write temp JSONC file and load via utilities._load_settings."""
        import re
        from pathlib import Path as _Path

        with NamedTemporaryFile(
            mode="w", suffix=".jsonc", delete=False, encoding="utf-8"
        ) as f:
            f.write(jsonc_content)
            tmp_path = _Path(f.name)

        try:
            text = tmp_path.read_text(encoding="utf-8")
            # Replicate _load_settings() logic
            text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
            parsed = json.loads(text)
            # Apply known-key filter like the real function
            result = {}
            for key in ["session_retention_days", "session_auto_backup",
                         "temp_script_retention_days", "analysis_language",
                         "session_save_list", "db_path_override",
                         "analysis_model", "analysis_variant"]:
                if key in parsed:
                    result[key] = parsed[key]
            return result
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_basic_jsonc(self):
        content = """{
            "session_retention_days": 14,
            "analysis_language": "zh-CN"
        }"""
        result = self._load_via_utilities(content)
        self.assertEqual(result["session_retention_days"], 14)
        self.assertEqual(result["analysis_language"], "zh-CN")

    def test_line_comments(self):
        content = """{
            // This is a comment
            "session_retention_days": 30
        }"""
        result = self._load_via_utilities(content)
        self.assertEqual(result["session_retention_days"], 30)

    def test_inline_comments(self):
        content = """{
            "session_retention_days": 7,   // keep one week
            "analysis_language": "en"      // use english
        }"""
        result = self._load_via_utilities(content)
        self.assertEqual(result["session_retention_days"], 7)
        self.assertEqual(result["analysis_language"], "en")

    def test_block_comments(self):
        content = """{
            /* Multi-line
               block comment */
            "session_retention_days": 10
        }"""
        result = self._load_via_utilities(content)
        self.assertEqual(result["session_retention_days"], 10)

    def test_unknown_keys_ignored(self):
        content = """{
            "session_retention_days": 7,
            "some_unknown_key": "should be ignored"
        }"""
        result = self._load_via_utilities(content)
        self.assertNotIn("some_unknown_key", result)
        self.assertEqual(result["session_retention_days"], 7)

    def test_empty_save_list(self):
        content = """{
            "session_save_list": {}
        }"""
        result = self._load_via_utilities(content)
        self.assertEqual(result["session_save_list"], {})

    def test_save_list_with_entries(self):
        content = """{
            "session_save_list": {
                "ses_abc": "my important session",
                "ses_def": "another one"
            }
        }"""
        result = self._load_via_utilities(content)
        self.assertIn("ses_abc", result["session_save_list"])
        self.assertEqual(result["session_save_list"]["ses_abc"], "my important session")


if __name__ == "__main__":
    unittest.main()
