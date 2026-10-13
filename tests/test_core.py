"""Exercise the production core without credentials, network, devices or subprocesses."""
import importlib
import sys
import unittest
from unittest.mock import MagicMock, patch


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {name: MagicMock() for name in (
            "numpy", "pyaudiowpatch", "requests", "spotipy", "spotipy.oauth2"
        )}
        cls.modules = patch.dict(sys.modules, modules)
        cls.modules.start()
        cls.core = importlib.import_module("reisync_core")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("reisync_core", None)
        cls.modules.stop()

    def test_lrc_metadata_is_ignored_and_minutes_are_converted(self):
        self.assertEqual(self.core.parse_lrc("[ar:Artist]\n[01:02.50] line \nplain"), [(62.5, "line")])

    def test_plain_lyrics_ignore_blank_lines(self):
        self.assertEqual(self.core.interpolate_plain_lyrics(" a\n\n b \n", 60), [(0, "a"), (30, "b")])
        self.assertEqual(self.core.interpolate_plain_lyrics(" \n", 60), [])

    def test_anchor_advances_only_while_playing_and_accepts_seek(self):
        anchor = self.core.PositionAnchor(10, 100)
        with patch.object(self.core.time, "monotonic", return_value=104):
            self.assertEqual(anchor.estimate_now(), 14)
            anchor.update(3, 103, playing=False)
            self.assertEqual(anchor.estimate_now(), 3)
            anchor.update(20, 103)
            self.assertEqual(anchor.estimate_now(), 21)

    def test_playback_position_uses_request_midpoint(self):
        spotify = MagicMock()
        spotify.current_playback.return_value = {
            "item": {"id": "track", "artists": [{"name": "Artist"}], "name": "Title", "duration_ms": 60000},
            "progress_ms": 12000, "is_playing": True,
        }
        with patch.object(self.core.time, "monotonic", side_effect=[100, 102]):
            playback = self.core.get_playback(spotify)
        self.assertEqual(playback["at"], 101)
        self.assertEqual(playback["position_sec"], 12)
        self.assertEqual(playback["duration_sec"], 60)

    def test_missing_playback_and_api_failure_return_none(self):
        spotify = MagicMock()
        spotify.current_playback.return_value = None
        self.assertIsNone(self.core.get_playback(spotify))
        spotify.current_playback.side_effect = RuntimeError("mock outage")
        self.assertIsNone(self.core.get_playback(spotify))

    def test_loopback_device_selects_matching_default_speaker(self):
        audio = MagicMock()
        audio.get_host_api_info_by_type.return_value = {"defaultOutputDevice": 1}
        audio.get_device_info_by_index.return_value = {"name": "Speakers", "isLoopbackDevice": False}
        matching = {"name": "Speakers [Loopback]", "isLoopbackDevice": True}
        audio.get_loopback_device_info_generator.return_value = iter([
            {"name": "Other", "isLoopbackDevice": True}, matching
        ])
        self.assertEqual(self.core.get_loopback_device(audio), matching)

    def test_missing_loopback_device_raises(self):
        audio = MagicMock()
        audio.get_host_api_info_by_type.return_value = {"defaultOutputDevice": 1}
        audio.get_device_info_by_index.return_value = {"name": "Speakers", "isLoopbackDevice": False}
        audio.get_loopback_device_info_generator.return_value = iter([])
        with self.assertRaisesRegex(RuntimeError, "Loopback"):
            self.core.get_loopback_device(audio)


if __name__ == "__main__":
    unittest.main()
