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
        return self.speech_and_end(samples)[0]

    def speech_and_end(self, samples: np.ndarray) -> tuple[np.ndarray, float | None]:
        """Parole concaténée, et fin de la dernière parole (secondes depuis le début ; None
        sans parole) : sert à mesurer l'attente entre la fin de la parole et la fin du flux."""
        if len(samples) == 0:
            return samples, None
        if self._vad is None:
            return _energy_vad(samples)
        last_end = 0
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
                segment = vad.front
                segments.append(np.asarray(segment.samples, dtype=np.float32))
                last_end = max(last_end, segment.start + len(segment.samples))
                vad.pop()
            vad.reset()
        if not segments:
            return np.zeros(0, dtype=np.float32), None
        # Le dernier segment peut déborder sur le silence ajouté : borné à l'audio reçu.
        return np.concatenate(segments), min(last_end, len(samples)) / SAMPLE_RATE


def _energy_vad(samples: np.ndarray, frame: int = 480) -> tuple[np.ndarray, float | None]:
    n = len(samples) // frame
    if n == 0:
        return samples, len(samples) / SAMPLE_RATE
    frames = samples[: n * frame].reshape(n, frame)
    rms = np.sqrt((frames**2).mean(axis=1) + 1e-12)
    # Seuil relatif au niveau de crête : -20 dB sous le 95e centile, et au moins -46 dBFS.
    keep = rms > max(0.1 * np.percentile(rms, 95), 0.005)
    kept = np.flatnonzero(keep)
    end = (kept[-1] + 1) * frame / SAMPLE_RATE if len(kept) else None
    return frames[keep].reshape(-1), end


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
