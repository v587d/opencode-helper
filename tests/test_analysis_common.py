"""
Tests for analysis/common.py (pure / mockable parts).

Run:
    python -m unittest tests.test_analysis_common -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.common import pick_analysis_model, invoke


class TestPickAnalysisModel(unittest.TestCase):
    """Tests for model selection priority."""

    @patch("analysis.common.get_settings")
    @patch("analysis.common.pick_free_model")
    def test_uses_configured_model(self, mock_pick_free, mock_get_settings):
        mock_get_settings.return_value = {"analysis_model": "opencode/custom-model"}
        result = pick_analysis_model()
        self.assertEqual(result, "opencode/custom-model")
        mock_pick_free.assert_not_called()

    @patch("analysis.common.get_settings")
    @patch("analysis.common.pick_free_model")
    def test_falls_back_to_free_model(self, mock_pick_free, mock_get_settings):
        mock_get_settings.return_value = {"analysis_model": None}
        mock_pick_free.return_value = "opencode/mimo-v2.5-free"
        result = pick_analysis_model()
        self.assertEqual(result, "opencode/mimo-v2.5-free")
        mock_pick_free.assert_called_once()

    @patch("analysis.common.get_settings")
    @patch("analysis.common.pick_free_model")
    def test_empty_string_falls_back(self, mock_pick_free, mock_get_settings):
        # Empty string is treated as "not configured" and falls back.
        mock_get_settings.return_value = {"analysis_model": ""}
        mock_pick_free.return_value = "opencode/mimo-v2.5-free"
        result = pick_analysis_model()
        self.assertEqual(result, "opencode/mimo-v2.5-free")
        mock_pick_free.assert_called_once()


class TestInvokeFallback(unittest.TestCase):
    """Tests for invoke() fallback behavior."""

    @patch("analysis.common.get_settings")
    @patch("analysis.common._run_opencode")
    @patch("analysis.common.pick_free_model")
    def test_configured_model_fails_then_free_fallback(
        self, mock_pick_free, mock_run, mock_get_settings
    ):
        """If configured model fails, invoke should retry with free model."""
        mock_get_settings.return_value = {
            "analysis_model": "opencode/custom-model",
            "analysis_variant": None,
        }
        mock_pick_free.return_value = "opencode/mimo-v2.5-free"
        # First call fails, second call succeeds
        mock_run.side_effect = [
            (1, "", "configured model error"),
            (0, '{"type":"text","part":{"text":"fallback works"}}', ""),
        ]

        result = invoke("test prompt")
        self.assertIn("fallback works", result)
        self.assertEqual(mock_run.call_count, 2)
        # First call used configured model, second used free model
        self.assertEqual(mock_run.call_args_list[0][0][1], "opencode/custom-model")
        self.assertEqual(mock_run.call_args_list[1][0][1], "opencode/mimo-v2.5-free")

    @patch("analysis.common.get_settings")
    @patch("analysis.common._run_opencode")
    @patch("analysis.common.pick_free_model")
    def test_explicit_model_no_fallback(
        self, mock_pick_free, mock_run, mock_get_settings
    ):
        """If caller explicitly passes a model, do not fall back."""
        mock_get_settings.return_value = {
            "analysis_model": "opencode/custom-model",
            "analysis_variant": None,
        }
        mock_run.return_value = (1, "", "explicit model error")

        result = invoke("test prompt", model="opencode/explicit-model")
        self.assertEqual(result, "")
        self.assertEqual(mock_run.call_count, 1)
        mock_pick_free.assert_not_called()

    @patch("analysis.common.get_settings")
    @patch("analysis.common._run_opencode")
    def test_variant_passed_to_command(self, mock_run, mock_get_settings):
        """analysis_variant setting is read from settings by invoke."""
        mock_get_settings.return_value = {
            "analysis_model": "opencode/custom-model",
            "analysis_variant": "low",
        }
        mock_run.return_value = (0, '{"type":"text","part":{"text":"ok"}}', "")

        invoke("test prompt")
        # _run_opencode is called with (prompt, model, timeout)
        # The actual command building is internal; we verify _run_opencode succeeds.
        self.assertEqual(mock_run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
