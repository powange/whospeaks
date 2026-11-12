"""Transcriptions hors du relais principal : connexion Wyoming séparée (anticipation, banc
d'essai) et API OpenAI du serveur Whisper (mode direct)."""

from __future__ import annotations

import logging
import time

import httpx
from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.event import Event

from .audio import SAMPLE_RATE, pcm16_to_float, wav_bytes

_LOGGER = logging.getLogger(__name__)


class SideConnections:
    """Compte les connexions de transcription ouvertes (aucune ne doit traîner)."""

    def __init__(self) -> None:
        self.open = 0


async def wyoming_transcribe(uri: str, transcribe: Event, start: Event, chunks: list[Event],
                             counter: SideConnections | None = None,
                             connect_timeout: float = 5.0) -> Event:
    """Transcription complète sur une connexion à part ; fermée dans tous les cas, y compris
    si la tâche est annulée (anticipation abandonnée)."""
    client = AsyncClient.from_uri(uri, connect_timeout=connect_timeout)
    await client.connect()
    if counter is not None:
        counter.open += 1
    try:
        await client.write_event(transcribe)
        await client.write_event(start)
        for chunk in chunks:
            await client.write_event(chunk)
        await client.write_event(AudioStop().event())
        while True:
            event = await client.read_event()
            if event is None:
                raise ConnectionError("Whisper a fermé la connexion sans texte")
            if Transcript.is_type(event.type):
                return event
            if event.type == "error":
                raise RuntimeError(f"Whisper : {event.data.get('text')}")
    finally:
        if counter is not None:
            counter.open -= 1
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def pcm_events(pcm: bytes, rate: int = SAMPLE_RATE, chunk_samples: int = 1024) -> tuple[Event, list[Event]]:
    """Audio 16 bits mono en événements Wyoming (début, morceaux)."""
    step = chunk_samples * 2
    start = AudioStart(rate=rate, width=2, channels=1).event()
    chunks = [AudioChunk(rate=rate, width=2, channels=1, audio=pcm[i:i + step]).event()
              for i in range(0, len(pcm), step)]
    return start, chunks


class OpenAITranscriber:
    """POST audio/transcriptions sur un serveur Whisper compatible OpenAI."""

    def __init__(self, base_url: str, model: str = "whisper-1", language: str = "fr",
                 timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.last_error: str | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0),
                                             limits=httpx.Limits(max_keepalive_connections=4))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(self, pcm: bytes, prompt: str = "", counter: SideConnections | None = None) -> str:
        """Texte de l'audio (16 kHz mono 16 bits). Lève une exception en cas d'échec."""
        data = {"model": self.model, "language": self.language, "response_format": "json",
                "temperature": "0"}
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("audio.wav", wav_bytes(pcm16_to_float(pcm)), "audio/wav")}
        if counter is not None:
            counter.open += 1
        t0 = time.perf_counter()
        try:
            resp = await self._http().post(f"{self.base_url}/audio/transcriptions", data=data, files=files)
            resp.raise_for_status()
            text = resp.json().get("text", "")
        except Exception as err:
            self.last_error = f"{type(err).__name__}: {err}"
            raise
        finally:
            if counter is not None:
                counter.open -= 1
        self.last_error = None
        _LOGGER.debug("Whisper (OpenAI) : %.0f ms", (time.perf_counter() - t0) * 1000)
        return text

    def info(self) -> dict:
        return {"mode": "openai", "url": self.base_url, "modele": self.model, "langue": self.language,
                "derniere_erreur": self.last_error}
