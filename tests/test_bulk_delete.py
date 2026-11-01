import pytest
from fastapi.testclient import TestClient

from tests.conftest import pcm, voice
from tests.test_silence import silence
from whospeaks.service import ServiceError
from whospeaks.web import create_app

PIERRE, LEA = 140.0, 230.0


def fill(service):
    """3 silences, 2 trop courtes, 1 reconnue, 1 inconnue."""
    pid = service.create_person("Pierre")
    service.add_audio_samples(pid, voice(PIERRE, 8.0, seed=1), "fichier")
    for i in range(3):
        service.record(service.analyze(silence(seed=i)), None)
    service.update_settings({"reprise_active": False})
    for i in range(2):
        service.record(service.analyze(pcm(PIERRE, 0.6, seed=i)), "oui")
    service.record(service.analyze(pcm(PIERRE, seed=5)), "allume")
    service.record(service.analyze(pcm(LEA, seed=5)), "bonjour")


def test_count_and_delete_only_that_filter(service):
    fill(service)
    clips = [service.phrase_clip(p["id"]) for p in service.list_phrases(filtre="ignoree_silence")]
    assert service.count_phrases("ignoree_silence") == 3
    assert service.delete_phrases("ignoree_silence") == 3
    assert service.count_phrases("ignoree_silence") == 0
    assert not any(c.exists() for c in clips)  # audio supprimé
    assert service.delete_phrases("trop_court") == 2
    # Le reste est intact : phrase reconnue, voix inconnue et son groupe, échantillons.
    assert {p["resultat"] for p in service.list_phrases()} == {"identifie", "inconnu"}
    assert len(service.list_clusters()) == 1
    assert service.list_persons()[0]["echantillons"] >= 1


def test_beyond_display_limit(service):
    for i in range(160):
        service.record(service.analyze(silence(0.5, seed=i)), None)
    assert len(service.list_phrases(150, filtre="ignoree_silence")) == 150
    assert service.delete_phrases("ignoree_silence") == 160


@pytest.mark.parametrize("filtre", ["identifie", "inconnu", "attribuee", "enrolement", "reprise", "", "x"])
def test_learning_filters_refused(service, filtre):
    with pytest.raises(ServiceError):
        service.delete_phrases(filtre)
    with pytest.raises(ServiceError):
        service.count_phrases(filtre)


def test_api(service):
    fill(service)
    client = TestClient(create_app(service, service.config))
    assert client.get("/api/phrases/compte?filtre=ignoree_silence").json()["nombre"] == 3
    assert client.delete("/api/phrases?filtre=identifie").status_code == 400
    r = client.delete("/api/phrases?filtre=ignoree_silence")
    assert r.status_code == 200 and r.json()["supprimees"] == 3
    assert client.delete("/api/phrases").status_code == 422  # filtre obligatoire
