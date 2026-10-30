import time

import numpy as np

from tests.conftest import pcm, voice
from whospeaks.audio import float_to_pcm16

PIERRE, LEA, TELE = 140.0, 230.0, 330.0


def test_fake_voices_are_separable(service):
    e = service.embedder
    a1, a2 = e.embed(voice(PIERRE, seed=1)), e.embed(voice(PIERRE, seed=2))
    b = e.embed(voice(LEA, seed=1))
    assert a1 @ a2 > 0.8 and a1 @ b < 0.4


def test_too_short(service):
    a = service.analyze(pcm(PIERRE, seconds=0.6))
    assert a.resultat == "trop_court" and a.etat == "trop court" and a.cluster_id is None
    service.record(a, "oui")
    assert service.list_clusters() == []


def test_unknown_voice_forms_one_group_then_recognized(service):
    for i in range(10):
        a = service.analyze(pcm(LEA, seed=i))
        assert a.resultat == "inconnu"
        service.record(a, f"phrase {i}")
    groups = service.list_clusters()
    assert len(groups) == 1 and groups[0]["taille"] == 10
    assert len(groups[0]["extraits"]) == 3
    lea = service.create_person("Léa", "person.lea")
    assert service.assign_cluster(groups[0]["id"], lea) == 10
    assert service.list_clusters() == []
    a = service.analyze(pcm(LEA, seed=99))
    assert a.resultat == "identifie" and a.nom == "Léa" and a.person_entity == "person.lea"
    payload = service.ha_payload(a, "allume la lumière")
    assert payload["state"] == "Léa" and payload["attributes"]["transcript"] == "allume la lumière"


def test_enrolement_then_auto_enrichment(service):
    pid = service.create_person("Pierre")
    events = []
    service.emit = lambda t, d: events.append((t, d))
    service.start_enrolement(pid, total=3)
    for i in range(3):
        a = service.analyze(pcm(PIERRE, seed=i))
        assert a.resultat == "enrolement"
        service.record(a, "x")
    assert service.enrolement is None
    assert [d["n"] for _, d in events] == [0, 1, 2, 3] and events[-1][1]["fin"] == "termine"
    persons = {p["nom"]: p for p in service.list_persons()}
    assert persons["Pierre"]["par_source"] == {"enrolement": 3}

    a = service.analyze(pcm(PIERRE, seed=10))
    assert a.resultat == "identifie" and a.score > 0.9
    service.record(a, "y")
    assert {p["nom"]: p for p in service.list_persons()}["Pierre"]["par_source"].get("auto") == 1


def test_enrolement_ignores_short_phrases(service):
    pid = service.create_person("Pierre")
    service.start_enrolement(pid, total=2)
    a = service.analyze(pcm(PIERRE, seconds=1.2))  # >= 1 s mais < 1,5 s
    assert a.resultat != "enrolement"
    assert service.enrolement.n == 0


def test_correction_moves_sample_and_removes_auto(service):
    pierre = service.create_person("Pierre")
    lea = service.create_person("Léa")
    service.add_audio_samples(pierre, voice(PIERRE, 8.0, seed=1), "fichier")
    service.add_audio_samples(lea, voice(LEA, 8.0, seed=1), "fichier")
    a = service.analyze(pcm(PIERRE, seed=5))
    assert a.nom == "Pierre"
    phrase = service.record(a, "test")
    counts = {p["nom"]: p["par_source"] for p in service.list_persons()}
    assert counts["Pierre"].get("auto") == 1
    # « Ce n'était pas Pierre mais Léa »
    service.assign_phrase(phrase, lea)
    counts = {p["nom"]: p["par_source"] for p in service.list_persons()}
    assert "auto" not in counts["Pierre"]
    assert counts["Léa"].get("attribution") == 1
    row = service.list_phrases(1)[0]
    assert row["personne_id"] == lea and row["etat"] == "attribuee"


def test_unassign_returns_to_unknown(service):
    pierre = service.create_person("Pierre")
    service.add_audio_samples(pierre, voice(PIERRE, 8.0, seed=1), "fichier")
    phrase = service.record(service.analyze(pcm(PIERRE, seed=3)), "x")
    service.unassign_phrase(phrase)
    row = service.list_phrases(1)[0]
    assert row["personne_id"] is None and row["cluster_id"] is not None
    assert "auto" not in service.list_persons()[0]["par_source"]


def test_ignored_group_does_not_reform(service):
    for i in range(4):
        service.record(service.analyze(pcm(TELE, seed=i)), "télé")
    gid = service.list_clusters()[0]["id"]
    service.ignore_cluster(gid)
    assert service.list_clusters() == []
    a = service.analyze(pcm(TELE, seed=50))
    assert a.ignoree and a.cluster_id is None
    service.record(a, "télé")
    assert service.list_clusters() == []
    assert service.list_phrases(1)[0]["etat"] == "ignoree"


def test_merge_delete_and_recluster(service):
    for i in range(3):
        service.record(service.analyze(pcm(LEA, seed=i)), "a")
        service.record(service.analyze(pcm(TELE, seed=i)), "b")
    groups = service.list_clusters()
    assert len(groups) == 2
    service.rename_cluster(groups[0]["id"], "Mamie")
    assert service.recluster() == 2
    assert "Mamie" in [g["nom"] for g in service.list_clusters()]
    g1, g2 = service.list_clusters()
    service.merge_clusters(g2["id"], g1["id"])
    groups = service.list_clusters()
    assert len(groups) == 1 and groups[0]["taille"] == 6
    service.delete_cluster(groups[0]["id"])
    assert service.list_clusters() == [] and service.list_phrases() == []


def test_rotation_caps_samples(service):
    service.update_settings({"max_echantillons": 5})
    pid = service.create_person("Pierre")
    service.add_audio_samples(pid, voice(PIERRE, 40.0, seed=1), "fichier")
    assert service.list_persons()[0]["echantillons"] == 5


def test_clips_and_retention(service, tmp_path):
    a = service.analyze(pcm(LEA, seed=1))
    a.horodatage = time.time() - 40 * 86400
    pid = service.record(a, "vieux")
    clip = service.phrase_clip(pid)
    assert clip.exists() and "clips/" in str(clip)
    assert service.purge_clips() == 1
    assert not clip.exists() and service.list_phrases(1)[0]["audio"] is False


def test_garder_audio_false(service):
    service.update_settings({"garder_audio": False})
    service.record(service.analyze(pcm(LEA, seed=1)), "x")
    assert service.list_phrases(1)[0]["audio"] is False
    assert not (service.config.data_dir / "clips").exists()


def test_calibrate(service):
    for name, f in (("Pierre", PIERRE), ("Léa", LEA), ("Paul", TELE)):
        pid = service.create_person(name)
        service.add_audio_samples(pid, voice(f, 24.0, seed=2), "fichier")
    r = service.calibrate()
    assert r["proposition"] is not None and r["personnes"] == 3
    assert set(r["confusion"]) == {"Pierre", "Léa", "Paul"}


def test_export_import_roundtrip(service, tmp_path):
    pid = service.create_person("Pierre")
    service.add_audio_samples(pid, voice(PIERRE, 8.0, seed=1), "fichier")
    archive = service.export_zip(tmp_path / "save.zip")
    service.delete_person(pid)
    assert service.list_persons() == []
    r = service.import_zip(archive)
    assert r["personnes"] == 1 and r["extraits"] == 2
    assert service.analyze(pcm(PIERRE, seed=4)).nom == "Pierre"


def test_corrupt_database_is_replaced(tmp_path):
    from whospeaks.config import Config
    from whospeaks.service import WhoSpeaks

    (tmp_path / "whospeaks.db").write_bytes(b"pas une base sqlite" * 100)
    svc = WhoSpeaks(Config(data_dir=tmp_path))
    assert svc.list_persons() == []
    assert list(tmp_path.glob("whospeaks.db.corrompue-*"))
    svc.db.close()


def test_no_model_gives_error_result(tmp_path):
    from whospeaks.config import Config
    from whospeaks.service import WhoSpeaks

    svc = WhoSpeaks(Config(data_dir=tmp_path))
    a = svc.analyze(float_to_pcm16(np.zeros(16000, dtype=np.float32)))
    assert a.resultat == "erreur" and a.etat == "inconnu"
    svc.db.close()
