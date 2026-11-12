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
        # Repris par le suivi de la parole en continu (une instance Silero par phrase)
        self.model_path = model_path
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


class SpeechTracker:
    """Suivi de la parole pendant que l'audio arrive (transcription anticipée).

    Alimenté par morceaux, dans un thread. Silero s'il est disponible (une instance par
    phrase), sinon l'énergie du signal. Les temps sont en secondes depuis le début de
    l'audio.
    """

    _FRAME = 480  # 30 ms, détecteur d'énergie

    def __init__(self, model_path: Path | None = None) -> None:
        self.position = 0  # échantillons reçus
        self.speech_samples = 0
        self.speech_end: int | None = None  # fin de la dernière parole repérée
        self.in_speech = False
        # Débuts et fins de parole (en échantillons), pour mesurer la parole après un instant
        self._spans: list[list[int]] = []
        self._pending = np.zeros(0, dtype=np.float32)
        self._vad = None
        self._peak = 0.0
        if model_path is not None:
            import sherpa_onnx

            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = str(model_path)
            config.silero_vad.threshold = 0.5
            config.silero_vad.min_silence_duration = 0.1
            config.silero_vad.min_speech_duration = 0.1
            config.silero_vad.max_speech_duration = 30
            config.sample_rate = SAMPLE_RATE
            config.num_threads = 1
            self._window = config.silero_vad.window_size
            self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)

    @property
    def position_s(self) -> float:
        return self.position / SAMPLE_RATE

    @property
    def speech_s(self) -> float:
        return self.speech_samples / SAMPLE_RATE

    @property
    def silence_s(self) -> float:
        """Silence depuis la fin de la dernière parole (0 pendant la parole ou sans parole)."""
        if self.in_speech or self.speech_end is None:
            return 0.0
        return (self.position - self.speech_end) / SAMPLE_RATE

    def speech_after(self, t_s: float) -> float:
        """Parole repérée après l'instant `t_s` (en secondes)."""
        start = int(t_s * SAMPLE_RATE)
        total = 0
        for a, b in self._spans:
            b = self.position if b is None else b
            total += max(0, b - max(a, start))
        return total / SAMPLE_RATE

    def feed(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32)
        buf = np.concatenate([self._pending, samples])
        step = self._window if self._vad is not None else self._FRAME
        n = len(buf) // step * step
        self._pending = buf[n:]
        for i in range(0, n, step):
            frame = buf[i : i + step]
            if self._vad is not None:
                self._vad.accept_waveform(frame)
                speech = self._vad.is_speech_detected()
                while not self._vad.empty():
                    self._vad.pop()  # segments inutiles ici : seul l'état compte
            else:
                rms = float(np.sqrt((frame**2).mean() + 1e-12))
                self._peak = max(self._peak, rms)
                speech = rms > max(0.1 * self._peak, 0.005)
            self._mark(speech, step)

    def _mark(self, speech: bool, step: int) -> None:
        start = self.position
        self.position += step
        if speech:
            self.speech_samples += step
            if not self.in_speech:
                self._spans.append([start, None])
            self.in_speech = True
        elif self.in_speech:
            self._spans[-1][1] = start
            self.speech_end = start
            self.in_speech = False
