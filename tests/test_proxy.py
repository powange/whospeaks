import asyncio
import statistics
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.event import async_read_event, async_write_event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info

from tests.conftest import pcm
from whospeaks.ha import HomeAssistant
from whospeaks.proxy import Proxy, ProxyHandler

TEXTS = [
    "Allume la lumière du salon.", "Éteins tout, s'il te plaît !", "Quelle heure est-il ?",
    "Mets « France Inter » sur l'enceinte", "Température : 21,5 °C ?", "  espaces  autour  ",
    "Ajoute du lait à la liste de courses", "Ferme les volets de la chambre", "Bonne nuit Jarvis 🌙",
    "Monte le chauffage d'un degré", "C'est quoi la météo demain ?", "Lance la playlist \"Jazz\"",
    "Rappelle-moi d'appeler Léa à 18h", "Ouvre le garage", "Combien font 12 × 7 ?",
    "Baisse le son", "Où est mon téléphone ?", "Arrête la minuterie", "Déclenche le scénario cinéma",
    "Merci !",
]
WHISPER_DELAY = 0.12


class FakeWhisper:
    def __init__(self):
        self.texts = list(TEXTS)
        self.received_bytes = 0
        self.server = None

    async def handle(self, reader, writer):
        try:
            while True:
                event = await async_read_event(reader)
                if event is None:
                    break
                if Describe.is_type(event.type):
                    info = Info(asr=[AsrProgram(
                        name="faster-whisper", attribution=Attribution("test", ""), installed=True,
                        description="Whisper de test", version="1",
                        models=[AsrModel(name="large-v3", attribution=Attribution("test", ""), installed=True,
                                         description=None, version=None, languages=["fr", "en"])])])
                    await async_write_event(info.event(), writer)
                elif AudioChunk.is_type(event.type):
                    self.received_bytes += len(event.payload or b"")
                elif AudioStop.is_type(event.type):
                    await asyncio.sleep(WHISPER_DELAY)
                    text = self.texts.pop(0) if self.texts else "fin"
                    await async_write_event(Transcript(text=text, language="fr").event(), writer)
        finally:
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


class FakeHA:
    def __init__(self):
        self.calls = []

    def transport(self):
        def handler(request: httpx.Request):
            self.calls.append((time.perf_counter(), request.url.path, request.read()))
            return httpx.Response(200, json={})
        return httpx.MockTransport(handler)


async def start_proxy(proxy):
    server = await asyncio.start_server(lambda r, w: ProxyHandler(r, w, proxy).run(), "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def transcribe(port, audio: bytes, rate=16000, width=2, channels=1):
    async with AsyncTcpClient("127.0.0.1", port) as client:
        await client.write_event(Transcribe(language="fr").event())
        await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
        step = 1024 * width * channels
        for i in range(0, len(audio), step):
            await client.write_event(AudioChunk(rate=rate, width=width, channels=channels,
                                                audio=audio[i:i + step]).event())
        t_stop = time.perf_counter()
        await client.write_event(AudioStop().event())
        while True:
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert event is not None, "connexion fermée sans transcript"
            if Transcript.is_type(event.type):
                return event, t_stop, time.perf_counter()


@pytest.fixture
async def whisper():
    w = FakeWhisper()
    port = await w.start()
    yield w, f"tcp://127.0.0.1:{port}"
    w.server.close()


def fake_ha():
    fake = FakeHA()
    ha = HomeAssistant("http://ha.test", "jeton", "sensor.jarvis_interlocuteur", "jarvis_interlocuteur")
    ha._client = httpx.AsyncClient(base_url="http://ha.test", transport=fake.transport())
    return fake, ha


async def test_transparency_and_ha_order(service, whisper):
    w, uri = whisper
    fake, ha = fake_ha()
    service.loop = asyncio.get_running_loop()
    proxy = Proxy(uri, service, ha)
    server, port = await start_proxy(proxy)
    audio = pcm(180.0, seconds=2.0)
    for expected in TEXTS:
        event, _, t_recv = await transcribe(port, audio)
        assert event.data["text"] == expected  # texte strictement identique
        assert event.data["text"].encode() == expected.encode()
        assert event.data["speaker"] == "inconnu"
        ha_times = [t for t, path, _ in fake.calls[-2:]]
        assert {p for _, p, _ in fake.calls[-2:]} == {
            "/api/states/sensor.jarvis_interlocuteur", "/api/events/jarvis_interlocuteur"}
        assert max(ha_times) < t_recv  # HA prévenu avant le renvoi du texte
    assert w.received_bytes == len(audio) * len(TEXTS)
    await asyncio.sleep(0.3)  # enregistrement en arrière-plan
    assert len(service.list_phrases(100)) == 20
    assert service.list_phrases(1)[0]["transcript"] == TEXTS[-1]
    server.close()
    await ha.close()


async def test_describe_copies_upstream(service, whisper):
    _, uri = whisper
    server, port = await start_proxy(Proxy(uri, service, None))
    async with AsyncTcpClient("127.0.0.1", port) as client:
        await client.write_event(Describe().event())
        info = Info.from_event(await client.read_event())
    assert info.asr[0].name == "whospeaks"
    assert info.asr[0].models[0].languages == ["fr", "en"]
    server.close()


async def test_describe_without_upstream(service):
    server, port = await start_proxy(Proxy("tcp://127.0.0.1:1", service, None))
    async with AsyncTcpClient("127.0.0.1", port) as client:
        await client.write_event(Describe().event())
        info = Info.from_event(await client.read_event())
    assert info.asr[0].models[0].languages == ["fr"]
    server.close()


async def test_transcript_without_service(whisper):
    _, uri = whisper
    server, port = await start_proxy(Proxy(uri, None, None))
    event, _, _ = await transcribe(port, pcm(180.0))
    assert event.data["text"] == TEXTS[0] and "speaker" not in event.data
    server.close()


async def test_transcript_when_identification_crashes(service, whisper):
    _, uri = whisper

    def boom(*_):
        raise RuntimeError("modèle cassé")

    service.analyze = boom
    server, port = await start_proxy(Proxy(uri, service, None))
    event, _, _ = await transcribe(port, pcm(180.0))
    assert event.data["text"] == TEXTS[0]
    server.close()


async def test_transcript_when_identification_is_slow(service, whisper):
    _, uri = whisper
    real = service.analyze

    def slow(*args):
        time.sleep(1.0)
        return real(*args)

    service.analyze = slow
    server, port = await start_proxy(Proxy(uri, service, None, id_extra_wait=0.15))
    event, t_stop, t_recv = await transcribe(port, pcm(180.0))
    assert event.data["text"] == TEXTS[0]
    assert t_recv - t_stop < WHISPER_DELAY + 0.15 + 0.2
    server.close()


async def test_transcript_when_ha_unreachable(service, whisper):
    _, uri = whisper
    ha = HomeAssistant("http://127.0.0.1:1", "jeton", "sensor.x", "x", timeout=0.3)
    server, port = await start_proxy(Proxy(uri, service, ha))
    event, t_stop, t_recv = await transcribe(port, pcm(180.0))
    assert event.data["text"] == TEXTS[0]
    assert t_recv - t_stop < WHISPER_DELAY + 0.3 + 0.2
    server.close()
    await ha.close()


async def test_transcript_without_model(tmp_path, whisper):
    from whospeaks.config import Config
    from whospeaks.service import WhoSpeaks

    _, uri = whisper
    svc = WhoSpeaks(Config(data_dir=tmp_path))  # aucun modèle chargé
    server, port = await start_proxy(Proxy(uri, svc, None))
    event, _, _ = await transcribe(port, pcm(180.0))
    assert event.data["text"] == TEXTS[0]
    server.close()
    svc.db.close()


async def test_upstream_down_returns_error(service):
    server, port = await start_proxy(Proxy("tcp://127.0.0.1:1", service, None, connect_timeout=0.5))
    async with AsyncTcpClient("127.0.0.1", port) as client:
        await client.write_event(Transcribe(language="fr").event())
        event = await asyncio.wait_for(client.read_event(), timeout=3)
    assert event.type == "error"
    server.close()


async def test_audio_48k_stereo_is_converted(service, whisper):
    w, uri = whisper
    server, port = await start_proxy(Proxy(uri, service, None))
    mono = np.frombuffer(pcm(180.0, seconds=2.0), dtype="<i2")
    up = np.repeat(mono, 3)  # 48 kHz approximatif
    stereo = np.stack([up, up], axis=1).astype("<i2").tobytes()
    event, _, _ = await transcribe(port, stereo, rate=48000, channels=2)
    assert event.data["speaker"] == "inconnu" and event.data["speaker_score"] is not None
    assert w.received_bytes == len(stereo)  # l'amont reçoit l'audio d'origine
    server.close()


ROOT = Path(__file__).resolve().parents[1] / ".cache"
MODELS = ROOT / "models"
SR_DATA = ROOT / "sr-data"
CAMPP = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
real = pytest.mark.skipif(
    not (MODELS / CAMPP).exists() or not SR_DATA.exists(),
    reason="modèle réel (.cache/models) ou voix de test (.cache/sr-data) absents",
)


def real_service(tmp_path):
    import shutil

    from whospeaks.config import Config
    from whospeaks.service import WhoSpeaks

    (tmp_path / "models").mkdir()
    for f in ("silero_vad.onnx", CAMPP):
        shutil.copy(MODELS / f, tmp_path / "models" / f)
    svc = WhoSpeaks(Config(data_dir=tmp_path))
    svc.load_models()
    assert svc.embedder is not None and svc.vad.is_silero
    return svc


@real
async def test_real_voices_identification_and_latency(tmp_path, whisper):
    """Vraies voix (sr-data) : enrôlement, identification, inconnus et temps ajouté médian < 50 ms."""
    from whospeaks.audio import float_to_pcm16, read_wav

    svc = real_service(tmp_path)
    for name in ("fangjun", "leijun", "liudehua"):
        pid = svc.create_person(name)
        for f in sorted((SR_DATA / "enroll").glob(f"{name}-*.wav")):
            svc.add_audio_samples(pid, read_wav(f), "fichier")
    w, uri = whisper
    fake, ha = fake_ha()
    server, port = await start_proxy(Proxy(uri, svc, ha))
    added, results = [], []
    files = sorted((SR_DATA / "test").glob("*.wav")) + sorted((SR_DATA / "test" / "3d-speaker").glob("*.wav"))
    for f in files:
        event, t_stop, t_recv = await transcribe(port, float_to_pcm16(read_wav(f)))
        added.append((t_recv - t_stop - WHISPER_DELAY) * 1000)
        results.append((f.name, event.data["speaker"]))
    for name, speaker in results:
        expected = name.split("-")[0] if "-test-" in name else "inconnu"
        assert speaker == expected, (name, speaker)
    med = statistics.median(added)
    print(f"temps ajouté médian (côté client) : {med:.1f} ms")
    assert med < 50
    server.close()
    await ha.close()
    svc.db.close()
