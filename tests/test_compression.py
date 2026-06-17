"""
Tests for the analysis compression layer (analysis/common.py helpers).

Covers the four opt-in compression primitives:
  - truncate()       : hard-cap a string with marker
  - head_tail()      : sample first N + last N chars
  - dedup_errors()   : fold similar error rows by prefix
  - enforce_budget() : drop lowest-priority sections when over total cap

Plus integration with render_prompt() and the settings.jsonc knobs.

All tests use @patch on get_settings to override the opt-in knobs
without mutating on-disk state.

Run:
    python -m unittest tests.test_compression -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.common import (
    truncate,
    head_tail,
    dedup_errors,
    enforce_budget,
    render_prompt,
)


# ═══════════════════════════════════════════════════════════════════
#  truncate()
# ═══════════════════════════════════════════════════════════════════

class TestTruncate(unittest.TestCase):
    """Hard-cap a string with a marker when it exceeds max_chars."""

    def test_passthrough_when_disabled(self):
        """max_chars=0 must return input unchanged (opt-in safety)."""
        self.assertEqual(truncate("any text", 0), "any text")
        self.assertEqual(truncate("x" * 10000, 0), "x" * 10000)

    def test_passthrough_when_short(self):
        """Strings within limit return unchanged."""
        self.assertEqual(truncate("short", 100), "short")
        self.assertEqual(truncate("exactly50" + "x" * 41, 50), "exactly50" + "x" * 41)

    def test_truncates_with_marker(self):
        """Over-length strings get a 'chars' marker suffix."""
        long = "x" * 500
        result = truncate(long, 50)
        self.assertLessEqual(len(result), 60)  # marker adds ~14 chars
        self.assertIn("chars", result)
        self.assertTrue(result.startswith("x"))  # content preserved

    def test_truncates_none_safely(self):
        """None input returns empty string (not a crash)."""
        self.assertEqual(truncate(None, 100), "")
        self.assertEqual(truncate("", 100), "")

    def test_truncate_exact_length_boundary(self):
        """String exactly at limit returns unchanged."""
        s = "a" * 50
        self.assertEqual(truncate(s, 50), s)


# ═══════════════════════════════════════════════════════════════════
#  head_tail()
# ═══════════════════════════════════════════════════════════════════

class TestHeadTail(unittest.TestCase):
    """Sample first N + last N characters; collapse the middle."""

    def test_short_passthrough(self):
        """Short strings pass through unchanged."""
        s = "short message"
        self.assertEqual(head_tail(s, head=80, tail=40), s)

    def test_preserves_head_and_tail(self):
        """Long strings keep their first head chars and last tail chars."""
        msg = "A" * 100 + "M" * 200 + "Z" * 100  # 400 chars
        result = head_tail(msg, head=50, tail=30)
        self.assertTrue(result.startswith("A" * 50))
        self.assertTrue(result.endswith("Z" * 30))
        self.assertIn("omitted", result)

    def test_omitted_count_accurate(self):
        """The 'omitted' marker should reflect the dropped middle length."""
        msg = "X" * 400  # 400 chars total
        result = head_tail(msg, head=80, tail=40)
        # omitted = 400 - 80 - 40 = 280
        self.assertIn("280 chars omitted", result)

    def test_none_and_empty(self):
        """None / empty inputs are handled safely."""
        self.assertEqual(head_tail(None), None)
        self.assertEqual(head_tail(""), "")
        # Short non-empty strings pass through
        self.assertEqual(head_tail("x"), "x")


# ═══════════════════════════════════════════════════════════════════
#  dedup_errors()
# ═══════════════════════════════════════════════════════════════════

class TestDedupErrors(unittest.TestCase):
    """Fold similar error rows by prefix; keep top-K unique patterns."""

    def test_passthrough_when_disabled(self):
        """prefix_len=0 must return rows unchanged."""
        rows = [("tool", "msg1", 5), ("tool", "msg2", 3)]
        self.assertEqual(dedup_errors(rows), rows)
        self.assertEqual(dedup_errors(rows, prefix_len=0, top_k=10), rows)

    def test_folds_matching_prefix(self):
        """Rows with matching prefixes get summed."""
        rows = [
            ("bash", "Error: file not found at /tmp/a.log", 5),
            ("bash", "Error: file not found at /tmp/b.log", 3),
            ("bash", "Error: file not found at /tmp/c.log", 2),
        ]
        result = dedup_errors(rows, prefix_len=25, top_k=10)
        # Common prefix is "Error: file not found at " (24 chars)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "bash")
        self.assertEqual(result[0][2], 10)  # 5+3+2

    def test_keeps_separate_per_tool(self):
        """Different tools with same prefix stay separate."""
        rows = [
            ("bash", "Error: oops", 1),
            ("read", "Error: oops", 1),
        ]
        result = dedup_errors(rows, prefix_len=20, top_k=10)
        self.assertEqual(len(result), 2)

    def test_top_k_creates_other_bucket(self):
        """Rows beyond top_k are folded into a synthetic '(other)' row."""
        rows = [("t", f"pat{i}: x", 1) for i in range(20)]
        result = dedup_errors(rows, prefix_len=5, top_k=3)
        self.assertEqual(len(result), 4)  # 3 kept + 1 other
        self.assertEqual(result[-1][0], "(other)")
        self.assertEqual(result[-1][2], 17)  # 20 - 3 = 17

    def test_no_other_when_within_top_k(self):
        """No 'other' bucket when total patterns <= top_k."""
        rows = [("t", f"pat{i}: x", 1) for i in range(3)]
        result = dedup_errors(rows, prefix_len=5, top_k=10)
        self.assertEqual(len(result), 3)
        # No '(other)' entry
        for r in result:
            self.assertNotEqual(r[0], "(other)")

    def test_sorts_by_count_descending(self):
        """Output is ordered by total count, highest first."""
        rows = [
            ("t", "rare: x", 1),
            ("t", "common: x", 100),
            ("t", "medium: x", 10),
        ]
        result = dedup_errors(rows, prefix_len=10, top_k=10)
        counts = [r[2] for r in result]
        self.assertEqual(counts, sorted(counts, reverse=True))


# ═══════════════════════════════════════════════════════════════════
#  enforce_budget()
# ═══════════════════════════════════════════════════════════════════

class TestEnforceBudget(unittest.TestCase):
    """Drop lowest-priority sections when prompt exceeds total cap."""

    def test_passthrough_when_disabled(self):
        """max_total_chars=0 returns input unchanged."""
        prompt = "## A\n" + "x" * 100000
        self.assertEqual(enforce_budget(prompt), prompt)

    @patch("analysis.common.get_settings")
    def test_under_budget_unchanged(self, mock_get_settings):
        """Prompts under the cap return unchanged."""
        mock_get_settings.return_value = {"analysis_max_total_chars": 10000}
        prompt = "## Section\nshort content"
        self.assertEqual(enforce_budget(prompt, command="harness"), prompt)

    @patch("analysis.common.get_settings")
    def test_drops_lowest_priority_section(self, mock_get_settings):
        """Harness priority: Session Lifecycle is dropped first."""
        mock_get_settings.return_value = {"analysis_max_total_chars": 500}
        prompt = (
            "## Intro\n" + "x" * 50 + "\n\n"
            "## Session Lifecycle\n" + "L" * 1000 + "\n\n"
            "## Session Status\n" + "S" * 50 + "\n\n"
            "## Efficiency Snapshot\n" + "E" * 50
        )
        result = enforce_budget(prompt, command="harness")
        self.assertLessEqual(len(result), 500)
        self.assertNotIn("Session Lifecycle", result)
        self.assertIn("## Intro", result)
        self.assertIn("Session Status", result)

    @patch("analysis.common.get_settings")
    def test_drops_tools_lowest_first(self, mock_get_settings):
        """Tools priority: Retry Chains, then Error Details, then Tool Distribution."""
        mock_get_settings.return_value = {"analysis_max_total_chars": 200}
        prompt = (
            "## Intro\n" + "i" * 20 + "\n\n"
            "## Retry Chains\n" + "R" * 500 + "\n\n"
            "## Error Details\n" + "E" * 500 + "\n\n"
            "## Tool Distribution\n" + "D" * 500 + "\n\n"
            "## Read:Edit Ratio\n" + "X" * 50
        )
        result = enforce_budget(prompt, command="tools")
        self.assertLessEqual(len(result), 250)
        self.assertNotIn("Retry Chains", result)
        self.assertNotIn("Error Details", result)
        self.assertNotIn("Tool Distribution", result)
        # Read:Edit Ratio is highest priority for tools, must survive
        self.assertIn("Read:Edit Ratio", result)

    @patch("analysis.common.get_settings")
    def test_unknown_command_falls_back(self, mock_get_settings):
        """Unknown command name doesn't crash; drops from end."""
        mock_get_settings.return_value = {"analysis_max_total_chars": 200}
        prompt = "## A\n" + "A" * 500 + "\n\n## B\n" + "B" * 50
        result = enforce_budget(prompt, command="unknown_cmd_xyz")
        # Should not crash; should still try to fit within budget
        self.assertLessEqual(len(result), 250)

    @patch("analysis.common.get_settings")
    def test_no_section_headers_hard_truncates(self, mock_get_settings):
        """Prompt without '## ' headers gets hard-truncated."""
        mock_get_settings.return_value = {"analysis_max_total_chars": 50}
        prompt = "x" * 500
        result = enforce_budget(prompt, command="harness")
        self.assertLessEqual(len(result), 60)
        self.assertIn("chars", result)


# ═══════════════════════════════════════════════════════════════════
#  render_prompt() with command kwarg
# ═══════════════════════════════════════════════════════════════════

class TestRenderPromptIntegration(unittest.TestCase):
    """Verify render_prompt wires through enforce_budget when command= is set."""

    @patch("analysis.common.get_settings")
    def test_no_command_skips_budget(self, mock_get_settings):
        """No command= kwarg means no budget enforcement."""
        mock_get_settings.return_value = {"analysis_max_total_chars": 10}
        result = render_prompt("harness", days="7", data="x" * 10000)
        # No budget enforcement: prompt includes all data
        self.assertIn("x" * 1000, result)

    @patch("analysis.common.get_settings")
    def test_command_activates_budget(self, mock_get_settings):
        """command='harness' activates budget enforcement."""
        mock_get_settings.return_value = {"analysis_max_total_chars": 500}
        long_data = "L" * 5000
        result = render_prompt(
            "harness",
            command="harness",
            days="7",
            data=long_data,
        )
        self.assertLessEqual(len(result), 600)


# ═══════════════════════════════════════════════════════════════════
#  Settings integration: opt-in defaults
# ═══════════════════════════════════════════════════════════════════

class TestCompressionDefaults(unittest.TestCase):
    """Verify all 5 new compression settings default to 0 (opt-in safety)."""

    def test_default_settings_have_all_compression_keys(self):
        """utilities._DEFAULT_SETTINGS must include the 5 new keys, all 0."""
        import utilities
        for key in (
            "analysis_max_error_chars",
            "analysis_max_total_chars",
            "analysis_max_rows_per_section",
            "analysis_error_dedup_prefix",
            "analysis_error_dedup_top_k",
        ):
            self.assertIn(key, utilities._DEFAULT_SETTINGS, f"missing key: {key}")
            self.assertEqual(
                utilities._DEFAULT_SETTINGS[key], 0,
                f"{key} default must be 0 (opt-in safety), got {utilities._DEFAULT_SETTINGS[key]!r}"
            )

    def test_helpers_respect_zero_setting(self):
        """With settings=0, only the SIZE-CAPPED helpers return unchanged.
        head_tail() and dedup_errors() are content-based, not size-based,
        so they always run their logic regardless of settings.
        """
        with patch("analysis.common.get_settings") as mock_gs:
            mock_gs.return_value = {
                "analysis_max_error_chars": 0,
                "analysis_max_total_chars": 0,
                "analysis_max_rows_per_section": 0,
                "analysis_error_dedup_prefix": 0,
                "analysis_error_dedup_top_k": 0,
            }
            # truncate: size-based, respects 0
            self.assertEqual(truncate("x" * 10000, 0), "x" * 10000)
            # dedup_errors: prefix-based, respects 0 (returns rows as-is)
            self.assertEqual(dedup_errors([("a", "b", 1)]), [("a", "b", 1)])
            # enforce_budget: size-based, respects 0
            self.assertEqual(enforce_budget("x" * 10000), "x" * 10000)


# ═══════════════════════════════════════════════════════════════════
#  End-to-end: each compression layer composes correctly
# ═══════════════════════════════════════════════════════════════════

class TestCompressionComposition(unittest.TestCase):
    """Verify truncate + head_tail compose correctly (the wired tools.py path)."""

    def test_truncate_then_head_tail(self):
        """The composition used in tools.py: error_msg -> head_tail(truncate(m, N))."""
        long = "A" * 500 + "M" * 200 + "Z" * 500  # 1200 chars

        # Step 1: truncate to 200 chars
        truncated = truncate(long, 200)
        self.assertLessEqual(len(truncated), 215)  # 200 + ~14 char marker

        # Step 2: head_tail the truncated result
        # Input is ~200 chars, head+tail+20 = 140; the function does
        # head_tail only when len > head+tail+20, so with truncated being
        # ~200 chars and head+tail+20=140, it WILL sample. Final is bounded
        # by head + tail + marker length (~20 chars).
        final = head_tail(truncated, head=80, tail=40)
        self.assertLessEqual(len(final), 150)  # 80 + 40 + ~22 char marker
        self.assertIn("omitted", final)

        # And the pipeline is always < 200 chars (well within the 200 budget)
        self.assertLessEqual(len(final), 200)


if __name__ == "__main__":
    unittest.main()
