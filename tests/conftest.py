import numpy as np
import pytest

from whospeaks.audio import SAMPLE_RATE, float_to_pcm16
from whospeaks.config import Config
from whospeaks.engine import Vad, normalize
from whospeaks.service import WhoSpeaks


class FakeEmbedder:
    """Empreinte = énergie par bande de fréquence : une « voix » = une fréquence."""

    key = "fake"
    dim = 64

    def embed(self, samples: np.ndarray) -> np.ndarray:
        spec = np.abs(np.fft.rfft(samples[: SAMPLE_RATE]))
        bands = np.array_split(spec[: 4000], self.dim)
        return normalize(np.array([b.sum() for b in bands], dtype=np.float32))


def voice(freq: float, seconds: float = 2.0, seed: int = 0, noise: float = 0.02) -> np.ndarray:
    """Signal synthétique : fondamentale + harmoniques + bruit."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    f = freq * (1 + rng.normal(0, 0.01))
    sig = 0.3 * np.sin(2 * np.pi * f * t) + 0.15 * np.sin(2 * np.pi * 2 * f * t) + 0.08 * np.sin(2 * np.pi * 3 * f * t)
    return (sig + rng.normal(0, noise, len(t))).astype(np.float32)


def pcm(freq: float, seconds: float = 2.0, seed: int = 0) -> bytes:
    return float_to_pcm16(voice(freq, seconds, seed))


@pytest.fixture
def service(tmp_path):
    svc = WhoSpeaks(Config(data_dir=tmp_path))
    svc.vad = Vad(None)
    svc.embedder = FakeEmbedder()
    svc.reload()
    yield svc
    svc.db.close()
