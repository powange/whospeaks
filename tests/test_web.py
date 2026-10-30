import io

import pytest
from fastapi.testclient import TestClient

from tests.conftest import voice
from whospeaks.audio import wav_bytes
from whospeaks.web import _is_local, create_app


@pytest.fixture
def client(service):
    service.config.ui_token = "secret"
    service.config.ha_url = "http://192.168.0.201:8123"
    return TestClient(create_app(service, service.config))


H = {"Authorization": "Bearer secret"}


def test_auth_required(client):
    assert client.get("/api/etat").status_code == 401
    assert client.get("/api/etat", headers={"X-Token": "faux"}).status_code == 401
    assert client.get("/api/etat", headers=H).status_code == 200
    assert client.get("/api/etat?token=secret").status_code == 200


def test_page_and_iframe_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "WhoSpeaks" in r.text
    assert "frame-ancestors 'self' http://192.168.0.201:8123" in r.headers["content-security-policy"]
    assert "x-frame-options" not in r.headers


def test_person_upload_and_enrolement(client, service):
    r = client.post("/api/personnes", json={"nom": "Pierre", "person_entity": "person.pierre"}, headers=H)
    pid = r.json()["id"]
    assert client.post("/api/personnes", json={"nom": "Pierre"}, headers=H).status_code == 400
    files = [("fichiers", ("a.wav", io.BytesIO(wav_bytes(voice(140.0, 6.0, seed=1))), "audio/wav")),
             ("fichiers", ("b.txt", io.BytesIO(b"pas de l'audio"), "text/plain"))]
    r = client.post(f"/api/personnes/{pid}/fichiers", files=files, headers=H).json()
    assert r["ajoutes"] == 2 and len(r["erreurs"]) == 1
    samples = client.get(f"/api/personnes/{pid}/echantillons", headers=H).json()
    assert client.get(f"/api/echantillons/{samples[0]['id']}/audio", headers=H).status_code == 200
    # Depuis HA : par nom, création si besoin
    r = client.post("/api/enrolement", json={"personne": "Léa", "n": 5}, headers=H).json()
    assert r == {"personne": "Léa", "total": 5}
    assert client.get("/api/etat", headers=H).json()["enrolement"]["personne"] == "Léa"
    client.delete("/api/enrolement", headers=H)
    assert client.get("/api/etat", headers=H).json()["enrolement"] is None


def test_journal_groups_and_settings(client, service):
    from tests.conftest import pcm

    for i in range(3):
        service.record(service.analyze(pcm(230.0, seed=i)), f"phrase {i}")
    phrases = client.get("/api/phrases", headers=H).json()
    assert len(phrases) == 3 and phrases[0]["cluster_nom"].startswith("Voix inconnue")
    assert client.get(f"/api/phrases/{phrases[0]['id']}/audio", headers=H).headers["content-type"] == "audio/wav"
    groups = client.get("/api/groupes", headers=H).json()
    r = client.post(f"/api/groupes/{groups[0]['id']}/attribuer", json={"nouveau_nom": "Léa"}, headers=H).json()
    assert r["phrases"] == 3
    assert client.get("/api/groupes", headers=H).json() == []

    assert client.put("/api/reglages", json={"inconnu": 1}, headers=H).status_code == 400
    r = client.put("/api/reglages", json={"seuil_identification": 0.6}, headers=H).json()
    assert r["seuil_identification"] == 0.6
    assert client.post("/api/calibrage", headers=H).status_code == 400  # une seule personne

    r = client.get("/api/export?audio=true", headers=H)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = client.post("/api/import", files={"archive": ("s.zip", io.BytesIO(r.content), "application/zip")}, headers=H)
    assert r.json()["personnes"] == 1


def test_is_local():
    assert _is_local("192.168.0.201") and _is_local("127.0.0.1") and _is_local("::ffff:10.0.0.2")
    assert _is_local("172.18.0.1")
    assert not _is_local("8.8.8.8") and not _is_local(None)
