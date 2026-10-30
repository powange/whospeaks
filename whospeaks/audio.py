"""Outils audio : conversion PCM, ré-échantillonnage, lecture et écriture WAV."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 16000


def pcm16_to_float(pcm: bytes) -> np.ndarray:
    """PCM 16 bits little-endian mono -> float32 dans [-1, 1]."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def resample(samples: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Ré-échantillonnage par FFT (suffisant pour des extraits de quelques secondes)."""
    if src_rate == dst_rate or len(samples) == 0:
        return samples.astype(np.float32, copy=False)
    n_out = int(round(len(samples) * dst_rate / src_rate))
    spectrum = np.fft.rfft(samples.astype(np.float64))
    n_bins = n_out // 2 + 1
    if n_bins <= len(spectrum):
        spectrum = spectrum[:n_bins]
    else:
        spectrum = np.concatenate([spectrum, np.zeros(n_bins - len(spectrum), dtype=spectrum.dtype)])
    out = np.fft.irfft(spectrum, n=n_out) * (n_out / len(samples))
    return out.astype(np.float32)


def to_mono(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 2:
        return samples.mean(axis=1)
    return samples


def decode_file(data: bytes) -> np.ndarray:
    """Décode un fichier audio (WAV, FLAC, OGG, MP3…) en float32 mono 16 kHz."""
    samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    return resample(to_mono(samples), rate, SAMPLE_RATE)


def read_wav(path: Path) -> np.ndarray:
    samples, rate = sf.read(str(path), dtype="float32", always_2d=False)
    return resample(to_mono(samples), rate, SAMPLE_RATE)


def write_wav(path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, rate, subtype="PCM_16")


def wav_bytes(samples: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, rate, subtype="PCM_16", format="WAV")
    return buf.getvalue()
