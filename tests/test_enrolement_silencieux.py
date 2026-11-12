import asyncio
import time

from tests.conftest import pcm
from tests.test_silence import run_one, silence
from tests.test_proxy import whisper  # noqa: F401 (fixture)

PIERRE = 140.0
LONG, SHORT = 2.0, 0.6


def test_flag_on_every_phrase_during_enrolement(service):
    pid = service.create_person("Pierre")
    service.start_enrolement(pid, total=5)
    assert service.analyze(pcm(PIERRE, LONG, seed=1)).pendant_enrolement  # comptée
    assert service.analyze(pcm(PIERRE, 1.2, seed=2)).pendant_enrolement  # < 1,5 s : non comptée
    assert service.analyze(pcm(PIERRE, SHORT, seed=3)).pendant_enrolement  # trop courte
    service.stop_enrolement()
    assert not service.analyze(pcm(PIERRE, LONG, seed=4)).pendant_enrolement


async def test_enrolement_is_silent_and_still_counts(service, whisper):  # noqa: F811
    w, uri = whisper
    pid = service.create_person("Pierre")
    service.start_enrolement(pid, total=2)
    w.texts = ["Il fait beau aujourd'hui", "euh", "Je lis une deuxième phrase", "Allume la lumière"]

    event, _, calls = await run_one(service, uri, pcm(PIERRE, LONG, seed=1), None)
    assert event.data["text"] == "" and event.data["ignored"] == "enrolement"
    assert calls == []  # ni capteur ni événement d'interlocuteur
    assert service.enrolement.n == 1
    row = service.list_phrases(1)[0]
    assert row["resultat"] == "enrolement" and row["transcript"] == "Il fait beau aujourd'hui"

    # Phrase trop courte pour compter : muette elle aussi, et non comptée.
    event, _, _ = await run_one(service, uri, pcm(PIERRE, SHORT, seed=2), None)
    assert event.data["text"] == "" and service.enrolement.n == 1

    # Dernière phrase : muette, et l'enrôlement se termine.
    event, _, _ = await run_one(service, uri, pcm(PIERRE, LONG, seed=3), None)
    assert event.data["text"] == "" and service.enrolement is None
    counts = service.list_persons()[0]["par_source"]
    assert counts == {"enrolement": 2}

    # Ensuite, Jarvis répond de nouveau normalement.
    event, _, calls = await run_one(service, uri, pcm(PIERRE, LONG, seed=4), None)
    assert event.data["text"] == "Allume la lumière" and event.data["speaker"] == "Pierre"
    await asyncio.sleep(0.05)
    assert {p for _, p, _ in calls} == {"/api/states/sensor.jarvis_interlocuteur", "/api/events/jarvis_interlocuteur"}


async def test_silence_during_enrolement_stays_silence(service, whisper):  # noqa: F811
    w, uri = whisper
    pid = service.create_person("Pierre")
    service.start_enrolement(pid, total=3)
    w.texts = [" ..."]
    event, _, _ = await run_one(service, uri, silence(), None)
    assert event.data["ignored"] == "silence" and service.enrolement.n == 0
    assert service.list_phrases(1)[0]["resultat"] == "ignoree_silence"


async def test_setting_disabled_lets_text_through(service, whisper):  # noqa: F811
    w, uri = whisper
    service.update_settings({"enrolement_silencieux": False})
    pid = service.create_person("Pierre")
    service.start_enrolement(pid, total=3)
    w.texts = ["Il fait beau aujourd'hui"]
    event, _, _ = await run_one(service, uri, pcm(PIERRE, LONG, seed=1), None)
    assert event.data["text"] == "Il fait beau aujourd'hui"
    assert service.enrolement.n == 1


async def test_independent_from_silence_filter(service, whisper):  # noqa: F811
    w, uri = whisper
    service.update_settings({"filtre_silence_actif": False})
    pid = service.create_person("Pierre")
    service.start_enrolement(pid, total=3)
    w.texts = ["Il fait beau aujourd'hui"]
    event, _, _ = await run_one(service, uri, pcm(PIERRE, LONG, seed=1), None)
    assert event.data["text"] == "" and event.data["ignored"] == "enrolement"


async def test_slow_analysis_uses_current_enrolement_state(service, whisper):  # noqa: F811
    w, uri = whisper
    pid = service.create_person("Pierre")
    service.start_enrolement(pid, total=3)
    real = service.analyze

    def slow(*args):
        time.sleep(0.6)
        return real(*args)

    service.analyze = slow
    w.texts = ["Il fait beau aujourd'hui"]
    event, _, _ = await run_one(service, uri, pcm(PIERRE, LONG, seed=1), None)
    assert event.data["text"] == "" and event.data["ignored"] == "enrolement"
    await asyncio.sleep(0.6)  # l'analyse finit et la phrase est comptée
    assert service.enrolement.n == 1
