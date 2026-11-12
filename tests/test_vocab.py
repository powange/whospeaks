import asyncio
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from tests.conftest import pcm, voice
from tests.test_proxy import FakeHA, start_proxy, transcribe, whisper  # noqa: F401 (fixture)
from tests.test_silence import silence
from whospeaks.audio import float_to_pcm16
from whospeaks.config import Config
from whospeaks.ha import HomeAssistant
from whospeaks.ha_ws import HaWsError, fetch_vocabulary, ws_url
from whospeaks.proxy import Proxy
from whospeaks.service import WhoSpeaks
from whospeaks.vocab import build_prompt, build_terms, estimate_tokens, parse_ha
from whospeaks.web import create_app

PIERRE = 140.0

# Réponses de l'API WebSocket de HA
EXPOSED = {"exposed_entities": {
    "switch.imprimante_3d": {"conversation": True},
    "switch.secheur": {"conversation": True},
    "light.garage": {"conversation": False},
}}
REGISTRY = [
    {"entity_id": "switch.imprimante_3d", "name": None, "original_name": "Prise", "aliases": ["Imprimante", "OctoPrint"]},
    {"entity_id": "switch.secheur", "name": "Sécheur de bobine", "aliases": []},
    {"entity_id": "light.garage", "name": "Lumière garage", "aliases": ["Néon du garage"]},
]
STATES = [
    {"entity_id": "switch.imprimante_3d", "attributes": {"friendly_name": "Imprimante 3D"}},
    {"entity_id": "light.garage", "attributes": {"friendly_name": "Lumière garage"}},
]
AREAS = [{"area_id": "salon", "name": "Salon", "aliases": ["Séjour"]}]
FLOORS = [{"floor_id": "etage", "name": "Étage", "aliases": []}]


def ha_vocab():
    return parse_ha(EXPOSED, REGISTRY, STATES, AREAS, FLOORS)


# --- Construction du vocabulaire ---------------------------------------------------
def test_vocabulary_from_ha_shared_entities_only():
    ha = ha_vocab()
    terms = [t for t, _ in build_terms(ha, ["Pierre"], ["Jarvis"], [])]
    assert "Imprimante 3D" in terms and "Imprimante" in terms and "OctoPrint" in terms  # nom et alias
    assert "Sécheur de bobine" in terms and "Salon" in terms and "Séjour" in terms and "Étage" in terms
    assert "Lumière garage" not in terms and "Néon du garage" not in terms  # non partagée avec Assist
    assert terms[:2] == ["Jarvis", "Pierre"]


def test_priority_and_dedup():
    terms = build_terms(ha_vocab(), ["Pierre"], ["Jarvis", "imprimante 3D"], ["imprimante"])
    cats = [c for _, c in terms]
    assert terms[0] == ("imprimante", "correction")
    assert cats.index("manuel") < cats.index("personne") < cats.index("alias") < cats.index("entite") < cats.index("piece")
    # « Imprimante 3D » (entité) déjà présent via les mots en plus : pas en double
    assert sum(1 for t, _ in terms if t.casefold() == "imprimante 3d") == 1


def test_prompt_truncated_with_corrections_first():
    long = [(f"appareil numéro {i}", "entite") for i in range(300)]
    terms = [("imprimante", "correction"), ("frotte-frotte", "manuel")] + long
    prompt, kept = build_prompt(terms, 200)
    assert estimate_tokens(prompt) <= 200 and len(kept) < len(terms)
    assert kept[:2] == ["imprimante", "frotte-frotte"] and prompt.startswith("imprimante, frotte-frotte, ")
    assert build_prompt([], 200) == ("", [])


# --- Service : prompt, cache, corrections ------------------------------------------
def test_service_prompt_and_cache(service, tmp_path):
    service.create_person("Pierre")
    service.update_settings({"mots_en_plus": ["Jarvis", "Ambilight"]})
    service.set_ha_vocabulary(ha_vocab())
    assert service.prompt.startswith("Jarvis, Ambilight, Pierre, ")
    assert "Imprimante 3D" in service.prompt and "Lumière garage" not in service.prompt
    # Cache sur disque : le vocabulaire HA survit à un redémarrage sans HA
    again = WhoSpeaks(Config(data_dir=service.config.data_dir))
    assert again.prompt == service.prompt and again.vocab_ha_maj is not None
    again.db.close()


def test_correction_adds_word_first(service):
    service.set_ha_vocabulary(ha_vocab())
    a = service.analyze(pcm(PIERRE))
    pid = service.record(a, "Allume l'impliment 3D et le sécheur de bobine.")
    mots = service.correct_text(pid, "Allume l'imprimante 3D et le sécheur de bobine")
    assert mots == [{"faux": "impliment", "vrai": "imprimante"}]
    assert service.prompt_terms[0] == "imprimante"
    assert service.list_phrases(1)[0]["transcript_corrige"] == "Allume l'imprimante 3D et le sécheur de bobine"
    assert service.list_phrases(1)[0]["transcript"] == "Allume l'impliment 3D et le sécheur de bobine."
    # Même correction renvoyée : pas comptée deux fois
    service.correct_text(pid, "Allume l'imprimante 3D et le sécheur de bobine")
    assert service.list_corrections()[0]["nombre"] == 1
    service.delete_correction("impliment", "imprimante")
    assert service.list_corrections() == [] and service.prompt_terms[0] != "imprimante"


def test_api_correction_and_vocabulary(service):
    client = TestClient(create_app(service, service.config))
    pid = service.record(service.analyze(pcm(PIERRE)), "Allume l'impliment 3D")
    r = client.post(f"/api/phrases/{pid}/texte", json={"texte": "Allume l'imprimante 3D"})
    assert r.json() == {"mots": [{"faux": "impliment", "vrai": "imprimante"}]}
    v = client.get("/api/vocabulaire").json()
    assert v["corrections"][0]["vrai"] == "imprimante" and v["termes"][0] == {
        "terme": "imprimante", "categorie": "correction", "libelle": "mal compris", "dans_prompt": True}
    assert client.get("/api/vocabulaire/prompt").text == service.prompt
    assert client.post(f"/api/phrases/{pid}/texte", json={"texte": "  "}).status_code == 400
    assert client.put("/api/reglages", json={"vocab_max_tokens": 500}).status_code == 400


# --- Proxy : termes, recopie du prompt, temps ---------------------------------------
def proxy_with(service, uri, fake=None):
    ha = None
    if fake is not None:
        import httpx
        ha = HomeAssistant("http://ha.test", "jeton", "sensor.jarvis_interlocuteur", "jarvis_interlocuteur")
        ha._client = httpx.AsyncClient(base_url="http://ha.test", transport=fake.transport())
    return Proxy(uri, service, ha), ha


async def test_terms_sent_upstream(service, whisper):  # noqa: F811
    w, uri = whisper
    service.update_settings({"mots_en_plus": ["Jarvis", "OctoPi"]})
    server, port = await start_proxy(Proxy(uri, service, None))
    await transcribe(port, pcm(PIERRE))
    assert w.transcribes[-1]["transcript_terms"][:2] == ["Jarvis", "OctoPi"]
    assert w.transcribes[-1]["language"] == "fr"
    server.close()


async def test_prompt_copy_is_hallucination(service, whisper):  # noqa: F811
    w, uri = whisper
    service.set_ha_vocabulary(ha_vocab())
    w.texts = [service.prompt, "Allume l'imprimante 3D"]
    server, port = await start_proxy(Proxy(uri, service, None))
    event, _, _ = await transcribe(port, pcm(PIERRE, 1.5))
    assert event.data["text"] == "" and event.data["ignored"] == "hallucination"
    await asyncio.sleep(0.3)
    assert service.list_phrases(1)[0]["resultat"] == "ignoree_hallucination"
    event, _, _ = await transcribe(port, pcm(PIERRE, 1.5))
    assert event.data["text"] == "Allume l'imprimante 3D"  # une vraie commande passe
    server.close()


async def test_prompt_copy_on_silence_is_emptied(service, whisper):  # noqa: F811
    w, uri = whisper
    service.set_ha_vocabulary(ha_vocab())
    w.texts = [service.prompt]
    server, port = await start_proxy(Proxy(uri, service, None))
    event, _, _ = await transcribe(port, silence())
    assert event.data["text"] == ""  # vide dans tous les cas (le silence répond même avant)
    server.close()


async def test_timings(service, whisper):  # noqa: F811
    w, uri = whisper
    w.delay = 0.5
    fake = FakeHA()
    proxy, ha = proxy_with(service, uri, fake)
    server, port = await start_proxy(proxy)
    # 1 s de parole puis 1 s de silence : HA n'a coupé le flux qu'une seconde après la parole
    audio = float_to_pcm16(np.concatenate([voice(PIERRE, 1.0, seed=1), np.zeros(16000, np.float32)]))
    await transcribe(port, audio)
    await asyncio.sleep(0.4)
    row = service.list_phrases(1)[0]
    assert 0.45 <= row["whisper_s"] <= 0.75
    assert row["duree_audio_s"] == pytest.approx(2.0, abs=0.01)
    assert row["attente_fin_s"] == pytest.approx(1.0, abs=0.1)
    assert row["duree_parole"] == pytest.approx(1.0, abs=0.1)
    assert row["identification_s"] is not None and row["attente_identification_s"] is not None
    assert row["total_s"] >= row["whisper_s"]
    sensors = {path: body for path, body in fake.timing_calls}
    assert sensors["/api/states/sensor.whospeaks_temps_whisper"]["state"] == pytest.approx(row["whisper_s"], abs=0.01)
    assert sensors["/api/states/sensor.whospeaks_temps_total"]["attributes"]["unit_of_measurement"] == "s"
    stats = TestClient(create_app(service, service.config)).get("/api/temps").json()
    assert stats[0]["whisper_s"]["mediane"] == pytest.approx(row["whisper_s"], abs=0.01)
    server.close()
    await ha.close()


async def test_timings_for_early_silence(service, whisper):  # noqa: F811
    w, uri = whisper
    server, port = await start_proxy(Proxy(uri, service, None))
    await transcribe(port, silence(2.0))
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["resultat"] == "ignoree_silence"
    assert row["whisper_s"] is None and row["total_s"] is not None  # réponse sans attendre Whisper
    server.close()


# --- Client WebSocket de HA (faux serveur) ------------------------------------------
async def fake_ha_ws(token="jeton", floors_ok=True):
    async def handler(ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != token:
            await ws.send(json.dumps({"type": "auth_invalid"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        answers = {
            "homeassistant/expose_entity/list": EXPOSED, "config/entity_registry/list": REGISTRY,
            "get_states": STATES, "config/area_registry/list": AREAS, "config/floor_registry/list": FLOORS,
        }
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "config/floor_registry/list" and not floors_ok:
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": False,
                                          "error": {"code": "unknown_command", "message": "Unknown command."}}))
                continue
            await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True,
                                      "result": answers[msg["type"]]}))
    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


async def test_fetch_vocabulary_over_websocket():
    server, url = await fake_ha_ws()
    data = await fetch_vocabulary(url, "jeton")
    assert [e["entity_id"] for e in data["entities"]] == ["switch.imprimante_3d", "switch.secheur"]
    assert data["entities"][0] == {"entity_id": "switch.imprimante_3d", "name": "Imprimante 3D",
                                   "aliases": ["Imprimante", "OctoPrint"]}
    assert data["floors"][0]["name"] == "Étage"
    server.close()


async def test_fetch_vocabulary_errors():
    server, url = await fake_ha_ws(floors_ok=False)
    assert (await fetch_vocabulary(url, "jeton"))["floors"] == []  # HA sans étages : toléré
    with pytest.raises(HaWsError, match="jeton"):
        await fetch_vocabulary(url, "faux")
    server.close()
    with pytest.raises(HaWsError):
        await fetch_vocabulary("http://127.0.0.1:1", "jeton", timeout=2)


def test_ws_url():
    assert ws_url("http://192.168.0.201:8123/") == "ws://192.168.0.201:8123/api/websocket"
    assert ws_url("https://ha.example") == "wss://ha.example/api/websocket"
