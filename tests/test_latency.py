"""Latence : la lecture de l'audio de HA ne doit jamais attendre Whisper ni l'identification."""

import asyncio
import socket
import time

import numpy as np
import pytest
import wyoming.client
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import async_read_event, async_write_event

from tests.conftest import voice
from tests.test_proxy import FakeHA, start_proxy, whisper  # noqa: F401 (fixture)
from whospeaks.audio import float_to_pcm16
from whospeaks.ha import HomeAssistant
from whospeaks.proxy import Proxy

PIERRE = 140.0
CHUNK = 1024  # échantillons : 64 ms, comme HA


async def ha_client(port, audio: bytes, rate=16000):
    """Comme HA : envoie l'audio au rythme réel, morceau par morceau, puis attend le texte.

    Renvoie (événement transcript, heure de l'audio-stop, heure du transcript)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await async_write_event(Transcribe(language="fr").event(), writer)
    await async_write_event(AudioStart(rate=rate, width=2, channels=1).event(), writer)
    step = CHUNK * 2
    t0 = time.perf_counter()
    for n, i in enumerate(range(0, len(audio), step)):
        # Envoi calé sur l'horloge de l'audio
        await asyncio.sleep(max(0.0, t0 + n * CHUNK / rate - time.perf_counter()))
        await async_write_event(AudioChunk(rate=rate, width=2, channels=1, audio=audio[i:i + step]).event(), writer)
    t_stop = time.perf_counter()
    await async_write_event(AudioStop().event(), writer)
    while True:
        event = await asyncio.wait_for(async_read_event(reader), timeout=10)
        assert event is not None
        if Transcript.is_type(event.type):
            t_recv = time.perf_counter()
            writer.close()
            return event, t_stop, t_recv


def phrase(seconds=3.0):
    return float_to_pcm16(np.concatenate([voice(PIERRE, seconds - 0.5, seed=1), np.zeros(8000, np.float32)]))


@pytest.fixture
def slow_relay(monkeypatch):
    """Écriture vers Whisper lente (0,1 s par morceau, plus lente que le temps réel)."""
    original = wyoming.client.AsyncClient.write_event

    async def slow(self, event):
        await asyncio.sleep(0.1)
        await original(self, event)
    monkeypatch.setattr(wyoming.client.AsyncClient, "write_event", slow)


async def test_ha_audio_read_on_time_with_slow_whisper(service, whisper, slow_relay):  # noqa: F811
    w, uri = whisper
    w.delay = 2.0
    server, port = await start_proxy(Proxy(uri, service, None))
    event, t_stop, t_recv = await ha_client(port, phrase(3.0))
    assert event.data["text"]
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    # L'audio de HA a été lu au rythme où il arrivait, malgré un relais 1,6 fois trop lent
    assert row["retard_audio_s"] < 0.05
    assert row["blocage_boucle_s"] < 0.05
    # Le retard s'est accumulé dans la file vers Whisper, pas côté HA, et il est mesuré
    assert row["relais_whisper_s"] > 1.0
    # Temps propre de Whisper, à 50 ms près
    assert row["whisper_s"] == pytest.approx(2.0, abs=0.05)
    assert t_recv - t_stop == pytest.approx(row["relais_whisper_s"] + row["whisper_s"], abs=0.1)
    server.close()


async def test_transcript_not_delayed_by_slow_identification(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 0.3
    real = service.analyze

    def slow(*args):
        time.sleep(1.0)  # empreinte vocale très lente
        return real(*args)
    service.analyze = slow
    fake = FakeHA()
    import httpx
    ha = HomeAssistant("http://ha.test", "jeton", "sensor.jarvis_interlocuteur", "jarvis_interlocuteur")
    ha._client = httpx.AsyncClient(base_url="http://ha.test", transport=fake.transport())
    server, port = await start_proxy(Proxy(uri, service, ha))
    event, t_stop, t_recv = await ha_client(port, phrase(2.0))
    # Texte renvoyé dès la réponse de Whisper (+ ID_EXTRA_WAIT au plus), pas après l'empreinte
    assert event.data["text"] and t_recv - t_stop < 0.3 + 0.15 + 0.1
    assert "speaker" not in event.data
    await asyncio.sleep(1.2)
    # Le capteur arrive quand même, une fois l'identification finie
    assert any(p == "/api/states/sensor.jarvis_interlocuteur" for _, p, _ in fake.calls)
    server.close()
    await ha.close()


async def test_timings_match_fake_whisper(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 0.5
    server, port = await start_proxy(Proxy(uri, service, None))
    _, t_stop, t_recv = await ha_client(port, phrase(2.0))
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["whisper_s"] == pytest.approx(0.5, abs=0.05)
    assert row["relais_whisper_s"] < 0.05
    assert row["duree_audio_s"] == pytest.approx(2.0, abs=0.07)
    assert row["retard_audio_s"] < 0.05
    assert row["attente_fin_s"] == pytest.approx(0.5, abs=0.15)
    assert row["total_s"] == pytest.approx(row["duree_audio_s"] + (t_recv - t_stop), abs=0.1)
    server.close()


async def test_tcp_nodelay_on_both_sockets(service, whisper):  # noqa: F811
    _, uri = whisper
    seen = []
    from whospeaks import proxy as proxymod
    original = proxymod._nodelay

    def spy(writer):
        original(writer)
        sock = writer.get_extra_info("socket") if writer is not None else None
        if sock is not None:
            seen.append(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
    proxymod._nodelay = spy
    try:
        server, port = await start_proxy(Proxy(uri, service, None))
        await ha_client(port, phrase(1.0))
    finally:
        proxymod._nodelay = original
    assert len(seen) == 2 and all(seen)  # socket de HA et socket vers Whisper
    server.close()
