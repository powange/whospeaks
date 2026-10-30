import asyncio
import time

import numpy as np
import pytest

from tests.conftest import pcm, voice
from tests.test_proxy import (  # noqa: F401 (fixture whisper)
    SR_DATA, WHISPER_DELAY, fake_ha, real, real_service, start_proxy, transcribe, whisper,
)
from whospeaks.audio import float_to_pcm16
from whospeaks.config import DEFAULT_SETTINGS
from whospeaks.filters import is_hallucination, normalize_text
from whospeaks.proxy import Proxy
from whospeaks.service import ServiceError

DEFAULTS = DEFAULT_SETTINGS["hallucinations"]
PIERRE, LEA = 140.0, 230.0


def silence(seconds=3.0, seed=0) -> bytes:
    rng = np.random.default_rng(seed)
    return float_to_pcm16(rng.normal(0, 0.001, int(seconds * 16000)).astype(np.float32))


# --- Filtre de texte ------------------------------------------------------------
def test_normalize_text():
    assert normalize_text("  Merci !  ") == "merci"
    assert normalize_text("Sous-titrage ST' 501") == "soustitragest501"
    assert normalize_text(" ... ♪ ") == ""
    assert normalize_text("Éteins") == "éteins"


@pytest.mark.parametrize("text", [
    "", " ", " ...", "...", "?!", "♪♪", "Merci.", "MERCI !", " merci",
    "Sous-titrage ST' 501", "Sous-titres réalisés par la communauté d'Amara.org",
])
def test_hallucinations(text):
    assert is_hallucination(text, DEFAULTS)


@pytest.mark.parametrize("text", [
    "Merci, éteins la lumière", "Allume la lumière.", "Merci beaucoup", "Stop",
])
def test_real_commands(text):
    assert not is_hallucination(text, DEFAULTS)


def test_regex_entries():
    entries = ["~^sous-titres? réalisés? par", "~(", "bonne nuit"]  # « ~( » invalide : ignorée
    assert is_hallucination("Sous-titres réalisés par Jean Dupont", entries)
    assert is_hallucination("Bonne nuit !", entries)
    assert not is_hallucination("Les sous-titres réalisés par Jean", entries)


# --- Service --------------------------------------------------------------------
def test_analyze_silence(service):
    a = service.analyze(silence())
    assert a.resultat == "ignoree_silence" and a.emb is None and a.duree_parole < 0.3


def test_analyze_silence_filter_disabled(service):
    service.update_settings({"filtre_silence_actif": False})
    assert service.analyze(silence()).resultat == "trop_court"


def test_silence_even_without_embedding_model(service):
    service.embedder = None  # VAD présent, modèle d'empreinte absent
    assert service.analyze(silence()).resultat == "ignoree_silence"
    assert service.analyze(pcm(PIERRE)).resultat == "erreur"


def test_record_hallucination_feeds_nothing(service):
    pid = service.create_person("Pierre")
    service.add_audio_samples(pid, voice(PIERRE, 8.0, seed=1), "fichier")
    before = service.db.one("SELECT COUNT(*) AS n FROM echantillons")["n"]
    a = service.analyze(pcm(PIERRE, seed=2))
    assert a.resultat == "identifie" and a.score >= 0.75  # aurait créé un échantillon auto
    a.resultat = "ignoree_hallucination"
    service.record(a, "Merci.")
    unknown = service.analyze(pcm(LEA, seed=2))
    assert unknown.cluster_id is not None
    unknown.resultat = "ignoree_hallucination"
    service.record(unknown, "Merci.")
    assert service.db.one("SELECT COUNT(*) AS n FROM echantillons")["n"] == before
    assert service.list_clusters() == []
    rows = service.list_phrases(filtre="ignoree_hallucination")
    assert len(rows) == 2 and all(r["etat"] == "ignoree" and r["personne_id"] is None for r in rows)
    status = service.status()
    assert status["ignorees"] == 2 and status["phrases"] == 0


@pytest.mark.parametrize("values", [
    {"silence_max_s": 2.5}, {"silence_max_s": -0.1}, {"filtre_silence_actif": 1},
    {"hallucinations": "merci"}, {"hallucinations": [1]}, {"hallucinations": ["~[a-"]},
])
def test_settings_validation(service, values):
    with pytest.raises(ServiceError):
        service.update_settings(values)


def test_settings_list_is_cleaned(service):
    s = service.update_settings({"hallucinations": [" merci ", "", "~^bonne nuit$"], "silence_max_s": 2})
    assert s["hallucinations"] == ["merci", "~^bonne nuit$"] and s["silence_max_s"] == 2


# --- Proxy de bout en bout -----------------------------------------------------
async def run_one(service, uri, audio, text):
    fake, ha = fake_ha()
    server, port = await start_proxy(Proxy(uri, service, ha))
    event, t_stop, t_recv = await transcribe(port, audio)
    await asyncio.sleep(0.3)  # enregistrement en arrière-plan
    server.close()
    await ha.close()
    return event, t_recv - t_stop, fake.calls


async def test_silence_returns_empty_without_waiting(service, whisper):  # noqa: F811
    w, uri = whisper
    w.texts = [" ..."]
    event, delay, calls = await run_one(service, uri, silence(), " ...")
    assert event.data["text"] == "" and event.data["ignored"] == "silence"
    assert delay < WHISPER_DELAY  # réponse avant même la fin de Whisper
    assert calls == []  # capteur non publié
    row = service.list_phrases(1)[0]
    assert row["resultat"] == "ignoree_silence" and row["audio"] is True


async def test_speech_text_unchanged(service, whisper):  # noqa: F811
    w, uri = whisper
    w.texts = ["Allume la lumière"]
    event, _, calls = await run_one(service, uri, pcm(PIERRE, seconds=1.0), None)
    assert event.data["text"] == "Allume la lumière"
    assert {p for _, p, _ in calls} == {"/api/states/sensor.jarvis_interlocuteur",
                                         "/api/events/jarvis_interlocuteur"}


async def test_merci_alone_is_emptied(service, whisper):  # noqa: F811
    w, uri = whisper
    w.texts = ["Merci."]
    event, _, calls = await run_one(service, uri, pcm(PIERRE, seconds=1.5), None)
    assert event.data["text"] == "" and event.data["ignored"] == "hallucination"
    assert calls == []
    row = service.list_phrases(1)[0]
    assert row["resultat"] == "ignoree_hallucination" and row["transcript"] == "Merci."


async def test_merci_with_command_unchanged(service, whisper):  # noqa: F811
    w, uri = whisper
    w.texts = ["Merci, éteins la lumière"]
    event, _, _ = await run_one(service, uri, pcm(PIERRE, seconds=1.5), None)
    assert event.data["text"] == "Merci, éteins la lumière"


async def test_filter_disabled_keeps_current_behaviour(service, whisper):  # noqa: F811
    service.update_settings({"filtre_silence_actif": False})
    w, uri = whisper
    w.texts = [" ...", "Merci."]
    event, _, calls = await run_one(service, uri, silence(), None)
    assert event.data["text"] == " ..." and len(calls) == 2
    event, _, _ = await run_one(service, uri, pcm(PIERRE, seconds=1.5), None)
    assert event.data["text"] == "Merci."


async def test_hallucination_filter_without_service_model(tmp_path, whisper):  # noqa: F811
    from whospeaks.config import Config
    from whospeaks.service import WhoSpeaks

    svc = WhoSpeaks(Config(data_dir=tmp_path))  # ni VAD ni modèle : pas de mesure du silence
    w, uri = whisper
    w.texts = [" ...", "Allume"]
    event, _, _ = await run_one(svc, uri, silence(), None)
    assert event.data["text"] == ""  # le texte reste filtré
    event, _, _ = await run_one(svc, uri, silence(), None)
    assert event.data["text"] == "Allume"  # sans VAD, le silence n'est pas détecté
    svc.db.close()


@real
async def test_real_silero_silence_and_short_speech(tmp_path, whisper):  # noqa: F811
    from whospeaks.audio import read_wav

    svc = real_service(tmp_path)
    w, uri = whisper
    w.texts = [" ...", "Allume la lumière"]
    # Comme la trace HA : 15 s sans parole. Pas d'assertion de délai ici : Silero met
    # ~75 ms sur 15 s d'audio, davantage sous charge (le test du faux VAD couvre la rapidité).
    event, _, _ = await run_one(svc, uri, silence(15.0), None)
    assert event.data["text"] == "" and event.data["ignored"] == "silence"
    clip = read_wav(SR_DATA / "test" / "fangjun-test-sr-2.wav")  # ≈ 1,4 s de parole
    event, _, _ = await run_one(svc, uri, float_to_pcm16(clip), None)
    assert event.data["text"] == "Allume la lumière"
    svc.db.close()
