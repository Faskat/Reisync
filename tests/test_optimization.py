"""Numerical equivalence and bounded lyric caching without live services/devices."""
import importlib
import sys
import unittest
from unittest.mock import MagicMock, patch

import numpy as np


class OptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = patch.dict(sys.modules, {
            name: MagicMock() for name in (
                'pyaudiowpatch', 'requests', 'spotipy', 'spotipy.oauth2'
            )
        })
        cls.modules.start()
        cls.core = importlib.import_module('reisync_core')

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('reisync_core', None)
        cls.modules.stop()

    def setUp(self):
        self.core._lyrics_cache.clear()
        self.core.requests.get.reset_mock(side_effect=True, return_value=True)

    def test_spectrum_matches_original_masks_for_supported_sample_rates(self):
        rng = np.random.default_rng(7)
        for rate in (8000, 16000, 44100, 48000, 96000, 192000):
            freqs = np.fft.rfftfreq(self.core.EQ_CHUNK, 1 / rate)
            edges = np.logspace(np.log10(50), np.log10(min(16000, rate / 2)),
                                self.core.EQ_BANDS + 1)
            masks = [(freqs >= lo) & (freqs < hi)
                     for lo, hi in zip(edges[:-1], edges[1:])]
            analyzer = self.core.SpectrumBands(rate)
            # Reuse the buffer: random audio, silence and energy at bin boundaries.
            for spectrum in (np.abs(np.fft.rfft(rng.normal(size=self.core.EQ_CHUNK))),
                             np.zeros(len(freqs)), np.arange(len(freqs), dtype=float)):
                expected = np.array([spectrum[m].mean() if m.any() else 0.0
                                     for m in masks])
                with self.subTest(rate=rate):
                    np.testing.assert_allclose(analyzer.means(spectrum), expected,
                                               rtol=1e-11, atol=1e-11)

    def respond(self, data, status=200):
        response = MagicMock(status_code=status)
        response.json.return_value = data
        self.core.requests.get.return_value = response

    def test_replay_uses_cache_and_does_not_return_mutable_cache_dict(self):
        self.respond({'syncedLyrics': '[00:01.00] Hello'})
        first = self.core.fetch_lyrics('Artist', 'Title', 60.9)
        first['syncedLyrics'] = 'modified by caller'
        self.assertEqual(self.core.fetch_lyrics('Artist', 'Title', 60.1),
                         {'syncedLyrics': '[00:01.00] Hello'})
        self.core.requests.get.assert_called_once()

    def test_cache_key_includes_artist_title_and_duration(self):
        self.respond({'plainLyrics': 'Hello'})
        for artist, title, duration in [('A', 'T', 60), ('B', 'T', 60),
                                        ('A', 'U', 60), ('A', 'T', 61)]:
            self.core.fetch_lyrics(artist, title, duration)
        self.assertEqual(self.core.requests.get.call_count, 4)

    def test_missing_lyrics_and_http_failures_are_retried(self):
        for data, status in [(None, 404), ({'syncedLyrics': None}, 200)]:
            with self.subTest(status=status):
                self.respond(data, status)
                self.core.requests.get.reset_mock()
                self.core.fetch_lyrics('A', 'T', 60)
                self.core.fetch_lyrics('A', 'T', 60)
                self.assertEqual(self.core.requests.get.call_count, 2)
                self.assertFalse(self.core._lyrics_cache)

    def test_expired_lyrics_are_refetched(self):
        self.respond({'plainLyrics': 'old'})
        with patch.object(self.core.time, 'monotonic', return_value=0):
            self.core.fetch_lyrics('A', 'T', 60)
        self.respond({'plainLyrics': 'new'})
        with patch.object(self.core.time, 'monotonic', return_value=self.core.LYRICS_CACHE_TTL):
            self.assertEqual(self.core.fetch_lyrics('A', 'T', 60), {'plainLyrics': 'new'})
        self.assertEqual(self.core.requests.get.call_count, 2)

    def test_cache_evicts_least_recently_used_entry(self):
        self.respond({'plainLyrics': 'Hello'})
        with patch.object(self.core, 'LYRICS_CACHE_SIZE', 2):
            for title in ('A', 'B', 'A', 'C'):
                self.core.fetch_lyrics('Artist', title, 60)
            self.assertEqual(len(self.core._lyrics_cache), 2)
            self.assertEqual(self.core.requests.get.call_count, 3)
            self.core.fetch_lyrics('Artist', 'B', 60)
            self.assertEqual(self.core.requests.get.call_count, 4)


def benchmark():
    """Compare the production analyzer with the former implementation offline."""
    from pathlib import Path
    import timeit

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    OptimizationTests.setUpClass()
    try:
        core = OptimizationTests.core
        freqs = np.fft.rfftfreq(core.EQ_CHUNK, 1 / 48000)
        edges = np.logspace(np.log10(50), np.log10(16000), core.EQ_BANDS + 1)
        masks = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges[:-1], edges[1:])]
        samples = np.random.default_rng(7).normal(size=core.EQ_CHUNK)
        window = np.hanning(core.EQ_CHUNK)
        spectrum = np.abs(np.fft.rfft(samples * window))
        analyzer = core.SpectrumBands(48000)

        def previous(spec):
            return np.array([spec[m].mean() if m.any() else 0.0 for m in masks])

        cases = {
            'bands_before': lambda: previous(spectrum),
            'bands_after': lambda: analyzer.means(spectrum),
            'fft_bands_before': lambda: previous(np.abs(np.fft.rfft(samples * window))),
            'fft_bands_after': lambda: analyzer.means(np.abs(np.fft.rfft(samples * window))),
        }
        print(f'Python {sys.version.split()[0]}, NumPy {np.__version__}; 48 kHz, 2048 samples')
        for name, callback in cases.items():
            micros = min(timeit.repeat(callback, number=1000, repeat=3)) * 1000
            print(f'{name}: {micros:.2f} us/block')
    finally:
        OptimizationTests.tearDownClass()
        sys.path.pop(0)


if __name__ == '__main__':
    if sys.argv[1:] == ['--benchmark']:
        benchmark()
    else:
        unittest.main()
