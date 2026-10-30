import asyncio
import logging

import pytest
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient

from tests.conftest import pcm, voice
from tests.test_proxy import start_proxy, whisper  # noqa: F401 (fixture)
from whospeaks.proxy import Proxy
from whospeaks.service import ServiceError

PIERRE, LEA = 140.0, 230.0
LONG, SHORT = 2.0, 0.6  # secondes de parole (seuil par défaut : 1,0 s)


@pytest.fixture
def pierre(service):
    pid = service.create_person("Pierre", "person.pierre")
    service.add_audio_samples(pid, voice(PIERRE, 8.0, seed=1), "fichier")
    return pid


def age_memory(service, seconds):
    """Fait comme si la dernière identification datait de `seconds` secondes."""
    for memo in service._memory.values():
        memo.horodatage -= seconds


def sample_count(service):
    return service.db.one("SELECT COUNT(*) AS n FROM echantillons")["n"]


def test_short_phrase_30s_later_is_reprise(service, pierre):
    first = service.analyze(pcm(PIERRE, LONG, seed=2))
    assert first.resultat == "identifie" and first.nom == "Pierre"
    age_memory(service, 30)
    a = service.analyze(pcm(LEA, SHORT, seed=3))
    assert a.resultat == "reprise" and a.etat == "Pierre" and a.person_id == pierre
    assert a.score == pytest.approx(first.score) and a.emb is None
    assert a.reprise_age == pytest.approx(30, abs=1)
    attrs = service.ha_payload(a, "stop")
    assert attrs["state"] == "Pierre"
    assert attrs["attributes"]["reprise"] is True and attrs["attributes"]["age_s"] == pytest.approx(30, abs=1)
    assert attrs["attributes"]["person_entity"] == "person.pierre"


def test_short_phrase_3min_later_is_too_short(service, pierre):
    service.analyze(pcm(PIERRE, LONG, seed=2))
    age_memory(service, 180)
    a = service.analyze(pcm(PIERRE, SHORT, seed=3))
    assert a.resultat == "trop_court" and a.etat == "trop court"
    assert service.ha_payload(a, "stop")["attributes"]["reprise"] is False


def test_short_phrase_without_previous_identification(service, pierre):
    assert service.analyze(pcm(PIERRE, SHORT)).resultat == "trop_court"


def test_reprise_does_not_refresh_memory(service, pierre):
    service.analyze(pcm(PIERRE, LONG, seed=2))
    age_memory(service, 100)
    assert service.analyze(pcm(PIERRE, SHORT)).resultat == "reprise"
    age_memory(service, 30)  # 130 s depuis l'identification, 30 s depuis la reprise
    assert service.analyze(pcm(PIERRE, SHORT)).resultat == "trop_court"


def test_no_cross_satellite_reprise(service, pierre):
    service.analyze(pcm(PIERRE, LONG, seed=2), satellite="bureau")
    assert service.analyze(pcm(PIERRE, SHORT), satellite="salon").resultat == "trop_court"
    assert service.analyze(pcm(PIERRE, SHORT), satellite="bureau").resultat == "reprise"
    # Satellite inconnu : entrée globale.
    assert service.analyze(pcm(PIERRE, SHORT)).resultat == "reprise"


def test_unknown_or_short_does_not_feed_memory(service, pierre):
    service.analyze(pcm(LEA, LONG, seed=2))  # inconnu
    assert service.analyze(pcm(PIERRE, SHORT)).resultat == "trop_court"


def test_reprise_never_creates_sample(service, pierre):
    service.analyze(pcm(PIERRE, LONG, seed=2))
    before = sample_count(service)
    for i in range(5):
        a = service.analyze(pcm(PIERRE, SHORT, seed=i))
        assert a.resultat == "reprise"
        service.record(a, "stop")
    assert sample_count(service) == before
    # Pas non plus en mode enrôlement : la phrase courte n'est pas comptée.
    service.start_enrolement(pierre, total=3)
    a = service.analyze(pcm(PIERRE, SHORT, seed=9))
    assert a.resultat == "reprise"
    service.record(a, "stop")
    assert sample_count(service) == before and service.enrolement.n == 0


def test_reprise_disabled_keeps_old_behaviour(service, pierre):
    service.update_settings({"reprise_active": False})
    service.analyze(pcm(PIERRE, LONG, seed=2))
    assert service.analyze(pcm(PIERRE, SHORT)).resultat == "trop_court"


def test_enrolement_feeds_memory(service):
    pid = service.create_person("Léa")
    service.start_enrolement(pid, total=5)
    assert service.analyze(pcm(LEA, LONG, seed=1)).resultat == "enrolement"
    a = service.analyze(pcm(LEA, SHORT))
    assert a.resultat == "reprise" and a.nom == "Léa"


def test_journal_correction_and_stats(service, pierre):
    lea = service.create_person("Léa")
    service.record(service.analyze(pcm(PIERRE, LONG, seed=2)), "quel temps fera-t-il demain")
    phrase = service.record(service.analyze(pcm(PIERRE, SHORT)), "allume la lumière")
    rows = service.list_phrases(filtre="reprise")
    assert [r["id"] for r in rows] == [phrase] and rows[0]["nom"] == "Pierre"
    status = service.status()
    assert status["reprises"] == 1 and status["phrases"] == 1
    before = sample_count(service)
    # « Corriger… » : change l'étiquette, sans créer d'échantillon (phrase trop courte).
    service.assign_phrase(phrase, lea)
    row = service.list_phrases(filtre="reprise")[0]
    assert row["nom"] == "Léa" and row["etat"] == "attribuee"
    assert sample_count(service) == before
    # « ✗ pas lui/elle » : redevient une phrase trop courte.
    service.unassign_phrase(phrase)
    assert service.list_phrases(1)[0]["resultat"] == "trop_court"


def test_deleted_person_is_not_resumed(service, pierre):
    service.analyze(pcm(PIERRE, LONG, seed=2))
    service.delete_person(pierre)
    assert service.analyze(pcm(PIERRE, SHORT)).resultat == "trop_court"


@pytest.mark.parametrize("values", [
    {"reprise_duree_s": 601}, {"reprise_duree_s": -1}, {"reprise_duree_s": "120"},
    {"reprise_active": "oui"}, {"reprise_duree_s": True},
])
def test_settings_validation(service, values):
    with pytest.raises(ServiceError):
        service.update_settings(values)


def test_settings_valid(service):
    s = service.update_settings({"reprise_active": False, "reprise_duree_s": 0})
    assert s["reprise_active"] is False and s["reprise_duree_s"] == 0
    assert service.update_settings({"reprise_duree_s": 600})["reprise_duree_s"] == 600


async def send(port, audio, context=None):
    async with AsyncTcpClient("127.0.0.1", port) as client:
        await client.write_event(Transcribe(language="fr", context=context).event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        await client.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=audio).event())
        await client.write_event(AudioStop().event())
        while True:
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            if Transcript.is_type(event.type):
                return event


async def test_proxy_passes_satellite_and_logs_context_once(service, pierre, whisper, caplog):  # noqa: F811
    _, uri = whisper
    server, port = await start_proxy(Proxy(uri, service, None))
    with caplog.at_level(logging.INFO, logger="whospeaks.proxy"):
        ev = await send(port, pcm(PIERRE, LONG, seed=2), context={"satellite_id": "bureau"})
        assert ev.data["speaker"] == "Pierre"
        ev = await send(port, pcm(PIERRE, SHORT), context={"satellite_id": "salon"})
        assert ev.data["speaker"] == "trop court"
        ev = await send(port, pcm(PIERRE, SHORT), context={"satellite_id": "bureau"})
        assert ev.data["speaker"] == "Pierre"
    assert "bureau" in service._memory
    assert [r.message for r in caplog.records if "satellite context" in r.message] == [
        "satellite context: present (['satellite_id'])"]
    server.close()


async def test_proxy_without_context_uses_global(service, pierre, whisper, caplog):  # noqa: F811
    _, uri = whisper
    server, port = await start_proxy(Proxy(uri, service, None))
    with caplog.at_level(logging.INFO, logger="whospeaks.proxy"):
        await send(port, pcm(PIERRE, LONG, seed=2))
        ev = await send(port, pcm(PIERRE, SHORT))
    assert ev.data["speaker"] == "Pierre"
    assert any("satellite context: absent" in r.message for r in caplog.records)
    server.close()
