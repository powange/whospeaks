"""VAD et calcul d'empreinte vocale (sherpa-onnx, CPU)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

_LOGGER = logging.getLogger(__name__)


def normalize(vec: np.ndarray) -> np.ndarray:
    vec = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


class Vad:
    """Silero VAD ; repli sur un détecteur d'énergie si le modèle est absent."""

    def __init__(self, model_path: Path | None) -> None:
        self._lock = threading.Lock()
        self._vad = None
        if model_path is not None:
            import sherpa_onnx

            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = str(model_path)
            config.silero_vad.threshold = 0.5
            config.silero_vad.min_silence_duration = 0.25
            config.silero_vad.min_speech_duration = 0.15
            config.silero_vad.max_speech_duration = 30
            config.sample_rate = SAMPLE_RATE
            config.num_threads = 1
            self._window = config.silero_vad.window_size
            self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)

    @property
    def is_silero(self) -> bool:
        return self._vad is not None

    def speech(self, samples: np.ndarray) -> np.ndarray:
        """Renvoie uniquement les passages de parole, concaténés."""
        if len(samples) == 0:
            return samples
        if self._vad is None:
            return _energy_vad(samples)
        with self._lock:
            vad = self._vad
            vad.reset()
            segments: list[np.ndarray] = []
            # Un peu de silence final pour que le dernier segment se ferme.
            padded = np.concatenate([samples, np.zeros(SAMPLE_RATE // 2, dtype=np.float32)])
            # Le détecteur doit recevoir l'audio fenêtre par fenêtre : d'un seul bloc,
            # il ne garde que le début de la parole.
            w = self._window
            for i in range(0, len(padded) - w + 1, w):
                vad.accept_waveform(padded[i : i + w])
            vad.flush()
            while not vad.empty():
                segments.append(np.asarray(vad.front.samples, dtype=np.float32))
                vad.pop()
            vad.reset()
        if not segments:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(segments)


def _energy_vad(samples: np.ndarray, frame: int = 480) -> np.ndarray:
    n = len(samples) // frame
    if n == 0:
        return samples
    frames = samples[: n * frame].reshape(n, frame)
    rms = np.sqrt((frames**2).mean(axis=1) + 1e-12)
    # Seuil relatif au niveau de crête : -20 dB sous le 95e centile, et au moins -46 dBFS.
    keep = rms > max(0.1 * np.percentile(rms, 95), 0.005)
    return frames[keep].reshape(-1)


class Embedder:
    def __init__(self, model_path: Path, key: str, num_threads: int = 4) -> None:
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=num_threads, provider="cpu"
        )
        if not config.validate():
            raise ValueError(f"Configuration de modèle invalide : {model_path}")
        self.key = key
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.dim = self._extractor.dim
        self._lock = threading.Lock()

    def embed(self, samples: np.ndarray) -> np.ndarray:
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(samples, dtype=np.float32))
            stream.input_finished()
            vec = self._extractor.compute(stream)
        return normalize(np.asarray(vec, dtype=np.float32))
