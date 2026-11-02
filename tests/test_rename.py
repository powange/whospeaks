import pytest
from fastapi.testclient import TestClient

from tests.conftest import pcm, voice
from whospeaks.service import ServiceError
from whospeaks.web import create_app

PIERRE = 140.0


@pytest.fixture
def pierre(service):
    pid = service.create_person("Pierre", "person.pierre")
    service.add_audio_samples(pid, voice(PIERRE, 8.0, seed=1), "fichier")
    return pid


def test_rename_keeps_samples_and_identification(service, pierre):
    before = service.list_persons()[0]["echantillons"]
    service.update_person(pierre, "  Pierrot  ", None)
    p = service.list_persons()[0]
    assert p["nom"] == "Pierrot" and p["echantillons"] == before and p["person_entity"] == "person.pierre"
    a = service.analyze(pcm(PIERRE, seed=2))
    assert a.resultat == "identifie" and a.nom == "Pierrot"
    assert service.ha_payload(a, "x")["state"] == "Pierrot"


def test_reprise_uses_new_name(service, pierre):
    service.analyze(pcm(PIERRE, seed=2))
    service.update_person(pierre, "Pierrot", None)
    assert service.analyze(pcm(PIERRE, 0.6, seed=3)).nom == "Pierrot"


def test_rename_refused(service, pierre):
    service.create_person("Léa")
    with pytest.raises(ServiceError, match="existe déjà"):
        service.update_person(pierre, "Léa", None)
    with pytest.raises(ServiceError, match="obligatoire"):
        service.update_person(pierre, "   ", None)
    assert service.list_persons()[1]["nom"] == "Pierre"


def test_rename_moves_ha_mapping_and_enrolement(service):
    pid = service.create_person("Pierre")  # sans entité : prise dans la correspondance des Réglages
    service.update_settings({"personnes_ha": {"Pierre": "person.pierre"}})
    service.start_enrolement(pid, total=5)
    service.update_person(pid, "Pierrot", None)
    assert service.settings["personnes_ha"] == {"Pierrot": "person.pierre"}
    assert service.db.load_settings({})["personnes_ha"] == {"Pierrot": "person.pierre"}
    assert service.list_persons()[0]["person_entity"] == "person.pierre"
    assert service.status()["enrolement"]["personne"] == "Pierrot"


def test_api_rename(service, pierre):
    client = TestClient(create_app(service, service.config))
    assert client.patch(f"/api/personnes/{pierre}", json={"nom": "Pierrot"}).json() == {"ok": True}
    assert client.get("/api/personnes").json()[0]["nom"] == "Pierrot"
    client.post("/api/personnes", json={"nom": "Léa"})
    assert client.patch(f"/api/personnes/{pierre}", json={"nom": "Léa"}).status_code == 400
