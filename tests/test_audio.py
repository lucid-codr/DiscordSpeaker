"""Audio routing regressions; no Discord connection or host audio changes."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

os.environ["DISCORD_BOT_TOKEN"] = "test-token-no-network"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bot


class AudioRoutingTests(unittest.TestCase):
    @patch.object(bot, "pulse_list", return_value=[{"index": 7, "name": "DiscordSpeakerBackup"}])
    def test_sink_name_must_match_exactly(self, pulse_list):
        self.assertFalse(bot.is_sink_active("DiscordSpeaker"))

    @patch.object(bot, "pulse_list")
    def test_status_resolves_numeric_sink_ids_and_deduplicates_apps(self, pulse_list):
        pulse_list.side_effect = [
            [{"index": 7, "name": "DiscordSpeaker"}],
            [
                {"sink": 7, "properties": {"application.name": "Firefox"}},
                {"sink": 7, "properties": {"application.name": "Firefox"}},
                {"sink": 8, "properties": {"application.name": "Spotify"}},
                {"sink": 7, "properties": {"application.process.binary": "vlc"}},
            ],
        ]
        self.assertEqual(bot.get_routed_apps(), ["Firefox", "vlc"])

    @patch.object(bot.subprocess, "run")
    @patch.object(bot, "pulse_list")
    @patch.object(bot, "is_sink_active", return_value=True)
    def test_route_any_app_moves_all_matching_streams(self, active, pulse_list, run):
        pulse_list.return_value = [
            {"index": 41, "properties": {"application.name": "Firefox"}},
            {"index": 42, "properties": {"application.process.binary": "firefox"}},
            {"index": 43, "properties": {"application.name": "Spotify"}},
        ]
        ok, message = bot.route_app_stream("  FIREFOX  ")
        self.assertTrue(ok)
        self.assertIn("2 playback", message)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["pactl", "move-sink-input", "41", "DiscordSpeaker"],
            ["pactl", "move-sink-input", "42", "DiscordSpeaker"],
        ])

    @patch.object(bot.subprocess, "run")
    @patch.object(bot, "pulse_list", return_value=[])
    @patch.object(bot, "is_sink_active", return_value=True)
    def test_no_active_app_returns_helpful_failure(self, active, pulse_list, run):
        ok, message = bot.route_app_stream("vlc")
        self.assertFalse(ok)
        self.assertIn("Start playback", message)
        run.assert_not_called()

    @patch.object(bot, "pulse_list")
    def test_empty_app_is_rejected_before_querying_audio(self, pulse_list):
        self.assertFalse(bot.route_app_stream("  ")[0])
        pulse_list.assert_not_called()

    @patch.object(bot, "ensure_sink_active", return_value=False)
    @patch.object(bot, "is_sink_active", return_value=False)
    def test_audio_server_failure_does_not_report_success(self, active, ensure):
        self.assertFalse(bot.route_app_stream("vlc")[0])

    @patch.object(bot.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pactl"))
    @patch.object(bot, "pulse_list", return_value=[{"index": 9, "properties": {"application.name": "VLC"}}])
    @patch.object(bot, "is_sink_active", return_value=True)
    def test_move_failure_is_reported(self, active, pulse_list, run):
        self.assertFalse(bot.route_app_stream("vlc")[0])

    @patch.object(bot, "route_app_stream", return_value=(True, "ok"))
    def test_spotify_remains_a_shortcut(self, route):
        self.assertEqual(bot.route_spotify_stream("CustomSink"), (True, "ok"))
        route.assert_called_once_with("spotify", "CustomSink")

    def test_general_and_spotify_commands_are_registered(self):
        self.assertIsNotNone(bot.bot.tree.get_command("route_app"))
        self.assertIsNotNone(bot.bot.tree.get_command("route_spotify"))
        self.assertIsNotNone(bot.bot.get_command("route_app"))


if __name__ == "__main__":
    unittest.main()
