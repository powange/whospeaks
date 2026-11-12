"""Transcription anticipée : Whisper démarre au premier silence, avant la fin du flux de HA."""

import asyncio
import json
import time

import httpx
import numpy as np
import pytest

from tests.conftest import voice
from tests.test_latency import ha_client
from tests.test_proxy import start_proxy, whisper  # noqa: F401 (fixture)
from whospeaks.audio import float_to_pcm16
from whospeaks.proxy import Proxy
from whospeaks.transcribe import OpenAITranscriber

PIERRE, LEA = 140.0, 230.0


def audio(*parts):
    """("v", secondes) : voix ; ("s", secondes) : silence."""
    out = []
    for i, (kind, seconds) in enumerate(parts):
        out.append(voice(PIERRE, seconds, seed=i) if kind == "v" else np.zeros(int(seconds * 16000), np.float32))
    return float_to_pcm16(np.concatenate(out))


async def test_anticipation_answers_at_audio_stop(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 1.0
    w.texts = ["Éteins la lumière", "Éteins la lumière"]
    server, port = await start_proxy(Proxy(uri, service, None))
    phrase = audio(("v", 1.5), ("s", 1.5))
    event, t_stop, t_recv = await ha_client(port, phrase)
    assert event.data["text"] == "Éteins la lumière"
    assert t_recv - t_stop < 0.25  # texte quasiment à l'audio-stop
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["anticipation"] == "utilisee" and row["anticipations_abandonnees"] == 0
    assert row["gain_anticipation_s"] == pytest.approx(1.0, abs=0.1)
    assert row["whisper_s"] == pytest.approx(1.0, abs=0.1)
    assert w.stops == 1  # Whisper n'a transcrit qu'une fois (connexion principale fermée sans fin)
    with_anticipation = row["total_s"]

    service.update_settings({"anticipation_active": False})
    event, t_stop, t_recv = await ha_client(port, phrase)
    assert t_recv - t_stop == pytest.approx(1.0, abs=0.1)
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["anticipation"] == "aucune"
    assert row["total_s"] - with_anticipation == pytest.approx(1.0, abs=0.15)  # ~1 s gagnée
    server.close()


async def test_pause_in_the_middle_abandons_then_completes(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 1.0
    w.texts = ["Allume", "Allume la lumière du salon"]
    proxy = Proxy(uri, service, None)
    server, port = await start_proxy(proxy)
    # Pause de 0,6 s : l'anticipation part (silence ≥ 0,4 s) puis la parole reprend
    event, t_stop, t_recv = await ha_client(port, audio(("v", 1.0), ("s", 0.6), ("v", 1.0), ("s", 1.5)))
    assert event.data["text"] == "Allume la lumière du salon"  # texte complet
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["anticipations_abandonnees"] == 1 and row["anticipation"] == "utilisee"
    # L'audio complet a été transcrit : la 2e anticipation commence au début du flux
    assert row["duree_parole"] == pytest.approx(2.0, abs=0.15)
    await asyncio.sleep(1.2)
    assert proxy.side.open == 0 and w.active == 0  # aucune connexion orpheline
    server.close()


async def test_resumed_speech_at_the_very_end_uses_full_transcription(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 0.3
    w.texts = ["Allume", "Allume la lumière"]
    proxy = Proxy(uri, service, None)
    server, port = await start_proxy(proxy)
    # La parole reprend juste avant l'audio-stop : pas le temps d'une nouvelle anticipation
    event, _, _ = await ha_client(port, audio(("v", 1.0), ("s", 0.6), ("v", 0.5)))
    assert event.data["text"] == "Allume la lumière"
    await asyncio.sleep(0.6)
    row = service.list_phrases(1)[0]
    assert row["anticipation"] == "abandonnee" and row["anticipations_abandonnees"] == 1
    assert proxy.side.open == 0 and w.active == 0
    server.close()


async def test_no_orphan_when_ha_leaves_mid_anticipation(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 2.0
    proxy = Proxy(uri, service, None)
    server, port = await start_proxy(proxy)
    from wyoming.asr import Transcribe
    from wyoming.audio import AudioChunk, AudioStart
    from wyoming.event import async_write_event
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await async_write_event(Transcribe(language="fr").event(), writer)
    await async_write_event(AudioStart(rate=16000, width=2, channels=1).event(), writer)
    pcm = audio(("v", 1.0), ("s", 0.8))
    t0 = time.perf_counter()
    for n, i in enumerate(range(0, len(pcm), 2048)):
        await asyncio.sleep(max(0.0, t0 + n * 0.064 - time.perf_counter()))
        await async_write_event(AudioChunk(rate=16000, width=2, channels=1, audio=pcm[i:i + 2048]).event(), writer)
    await asyncio.sleep(0.2)
    assert proxy.side.open == 1  # anticipation en cours
    writer.close()  # HA abandonne le run sans audio-stop
    await asyncio.sleep(0.3)
    assert proxy.side.open == 0  # WhoSpeaks a fermé sa connexion de transcription anticipée
    # Whisper, lui, ne le voit qu'en répondant (ici après 2 s), comme wyoming_openai
    await asyncio.sleep(2.0)
    assert w.active == 0
    server.close()


async def test_abandoned_anticipation_leaves_no_trace(service, whisper):  # noqa: F811
    """L'identification anticipée abandonnée ne crée ni groupe ni mémoire de reprise."""
    w, uri = whisper
    w.delay = 0.3
    w.texts = ["a", "b", "c"]
    server, port = await start_proxy(Proxy(uri, service, None))
    await ha_client(port, audio(("v", 1.0), ("s", 0.6), ("v", 1.0), ("s", 0.8)))
    await asyncio.sleep(0.6)
    groups = service.list_clusters()
    assert len(groups) == 1 and groups[0]["taille"] == 1  # une seule phrase, pas l'abandonnée
    server.close()


# --- Mode direct : API OpenAI du serveur Whisper -----------------------------------
def fake_openai(delay=0.2, text="Éteins la lumière"):
    seen = []

    async def handler(request: httpx.Request):
        body = request.read()
        seen.append(body)
        await asyncio.sleep(delay)
        return httpx.Response(200, json={"text": text})
    t = OpenAITranscriber("http://whisper.test/v1", "whisper-1", "fr")
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t, seen


async def test_direct_openai_mode(service):
    service.update_settings({"mots_en_plus": ["Jarvis", "OctoPi"], "anticipation_active": False})
    openai, seen = fake_openai(0.3)
    # Pas de Wyoming : l'adresse ne répond pas, elle ne doit pas servir
    proxy = Proxy("tcp://127.0.0.1:1", service, None, openai=openai, direct=True)
    server, port = await start_proxy(proxy)
    event, t_stop, t_recv = await ha_client(port, audio(("v", 1.2), ("s", 0.3)))
    assert event.data["text"] == "Éteins la lumière"
    assert t_recv - t_stop == pytest.approx(0.3, abs=0.15)
    body = seen[0]
    assert b'name="prompt"' in body and "Jarvis, OctoPi".encode() in body  # vocabulaire par requête
    assert b'name="language"' in body and b"RIFF" in body  # WAV
    info = (await proxy.get_info()).asr[0]
    assert info.models[0].name == "whisper-1"
    await asyncio.sleep(0.3)
    assert service.list_phrases(1)[0]["whisper_s"] == pytest.approx(0.3, abs=0.1)
    server.close()


async def test_direct_openai_mode_with_anticipation(service):
    openai, seen = fake_openai(0.8)
    proxy = Proxy("tcp://127.0.0.1:1", service, None, openai=openai, direct=True)
    server, port = await start_proxy(proxy)
    event, t_stop, t_recv = await ha_client(port, audio(("v", 1.2), ("s", 1.5)))
    assert event.data["text"] == "Éteins la lumière" and t_recv - t_stop < 0.25
    assert len(seen) == 1
    server.close()


async def test_direct_openai_failure_reports_error(service):
    service.update_settings({"anticipation_active": False})

    async def handler(request):
        return httpx.Response(500, json={"error": "panne"})
    openai = OpenAITranscriber("http://whisper.test/v1")
    openai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    server, port = await start_proxy(Proxy("tcp://127.0.0.1:1", service, None, openai=openai, direct=True))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    from wyoming.asr import Transcribe
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.event import async_read_event, async_write_event
    await async_write_event(Transcribe(language="fr").event(), writer)
    await async_write_event(AudioStart(rate=16000, width=2, channels=1).event(), writer)
    await async_write_event(AudioChunk(rate=16000, width=2, channels=1, audio=audio(("v", 1.0))).event(), writer)
    await async_write_event(AudioStop().event(), writer)
    event = await asyncio.wait_for(async_read_event(reader), 5)
    assert event.type == "error"
    writer.close()
    server.close()


# --- Banc d'essai -------------------------------------------------------------------
async def test_bench_measures_each_path(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 0.1
    openai, _ = fake_openai(0.05)
    proxy = Proxy(uri, service, None, openai=openai, direct=False)
    assert proxy.start_bench(essais=2)
    assert not proxy.start_bench()  # un seul à la fois
    for _ in range(100):
        await asyncio.sleep(0.1)
        if proxy.bench["etat"] != "en_cours":
            break
    b = proxy.bench
    assert b["etat"] == "termine" and b["progression"] == 1.0
    assert [(r["chemin"], r["fichier"]) for r in b["resultats"]] == [
        ("wyoming", "fr_2s.wav"), ("wyoming", "fr_4s.wav"), ("wyoming", "fr_8s.wav"),
        ("openai", "fr_2s.wav"), ("openai", "fr_4s.wav"), ("openai", "fr_8s.wav")]
    wyo = b["resultats"][0]
    assert len(wyo["essais_s"]) == 2 and wyo["mediane_s"] == pytest.approx(0.1, abs=0.08)
    assert b["resultats"][3]["mediane_s"] == pytest.approx(0.05, abs=0.05)
    assert all(r["erreur"] is None for r in b["resultats"])
    assert proxy.side.open == 0
    json.dumps(b)  # renvoyé tel quel par l'API


from tests.test_proxy import SR_DATA, real, real_service  # noqa: E402


@real
async def test_anticipation_with_real_voice_and_silero(tmp_path, whisper):  # noqa: F811
    """Vraie voix, vrai Silero, envoi au rythme réel : 1,5 s de silence avant l'audio-stop."""
    from whospeaks.audio import read_wav

    svc = real_service(tmp_path)
    w, uri = whisper
    w.delay = 0.8
    w.texts = ["phrase"] * 3
    proxy = Proxy(uri, svc, None)
    server, port = await start_proxy(proxy)
    clip = read_wav(SR_DATA / "test" / "fangjun-test-sr-1.wav")
    pcm = float_to_pcm16(np.concatenate([clip, np.zeros(24000, np.float32)]))
    event, t_stop, t_recv = await ha_client(port, pcm)
    assert event.data["text"] == "phrase" and t_recv - t_stop < 0.25
    await asyncio.sleep(0.5)
    row = svc.list_phrases(1)[0]
    assert row["anticipation"] == "utilisee" and row["gain_anticipation_s"] == pytest.approx(0.8, abs=0.1)
    assert proxy.side.open == 0
    server.close()
    svc.db.close()
