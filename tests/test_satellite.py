import asyncio
import json
import logging
import sqlite3
import time
from types import SimpleNamespace

import httpx
import jinja2
from fastapi.testclient import TestClient
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient

from tests.conftest import pcm, voice
from tests.test_proxy import WHISPER_DELAY, FakeHA, start_proxy, whisper  # noqa: F401 (fixture)
from tests.test_silence import silence
from whospeaks import db as dbmod
from whospeaks.ha import SATELLITE_TEMPLATE, HomeAssistant
from whospeaks.proxy import Proxy
from whospeaks.web import create_app

SALON = {"entity_id": "assist_satellite.vban_satellite_salon", "nom": "VBAN Satellite Salon", "piece": "Salon"}
BUREAU = {"entity_id": "assist_satellite.vban_satellite_bureau", "nom": "VBAN Satellite Bureau", "piece": "Bureau"}
PIERRE = 140.0


# --- Le template envoyé à HA (rendu avec Jinja2 et de faux états) ----------------
def render(satellites, areas=None):
    env = jinja2.Environment()
    env.filters["to_json"] = json.dumps
    states = SimpleNamespace(assist_satellite=[SimpleNamespace(**s) for s in satellites])
    out = env.from_string(SATELLITE_TEMPLATE).render(
        states=states, area_name=lambda e: (areas or {}).get(e))
    return json.loads(out) if out.strip() else None


def sat(entity, state, changed, name):
    return {"entity_id": entity, "state": state, "last_changed": changed, "name": name}


def test_template_one_active():
    got = render([sat("assist_satellite.salon", "listening", "2026-11-02T10:00:00", "Salon VBAN"),
                  sat("assist_satellite.bureau", "idle", "2026-11-02T10:05:00", "Bureau VBAN")],
                 {"assist_satellite.salon": "Salon"})
    assert got == {"entity_id": "assist_satellite.salon", "nom": "Salon VBAN", "piece": "Salon"}


def test_template_most_recent_wins():
    got = render([sat("assist_satellite.salon", "processing", "2026-11-02T10:00:00", "Salon VBAN"),
                  sat("assist_satellite.bureau", "listening", "2026-11-02T10:00:05", "Bureau VBAN")])
    assert got["entity_id"] == "assist_satellite.bureau" and got["piece"] == ""


def test_template_none_active():
    assert render([sat("assist_satellite.salon", "idle", "2026-11-02T10:00:00", "Salon VBAN"),
                   sat("assist_satellite.bureau", "responding", "2026-11-02T10:00:05", "Bureau VBAN")]) is None
    assert render([]) is None


# --- active_satellite ------------------------------------------------------------
def ha_with(handler):
    ha = HomeAssistant("http://ha.test", "jeton", "sensor.x", "x")
    ha._client = httpx.AsyncClient(base_url="http://ha.test", transport=httpx.MockTransport(handler))
    return ha


async def test_active_satellite_found_and_absent():
    seen = []

    def handler(request):
        seen.append(json.loads(request.read())["template"])
        return httpx.Response(200, text=json.dumps(SALON))
    assert await ha_with(handler).active_satellite() == SALON
    assert seen == [SATELLITE_TEMPLATE]
    assert await ha_with(lambda r: httpx.Response(200, text="  ")).active_satellite() is None
    assert await ha_with(lambda r: httpx.Response(200, text="pas du json")).active_satellite() is None
    assert await ha_with(lambda r: httpx.Response(401)).active_satellite() is None


async def test_active_satellite_unreachable_or_slow(caplog):
    def refused(request):
        raise httpx.ConnectError("connexion refusée")

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, text=json.dumps(SALON))

    with caplog.at_level(logging.WARNING, logger="whospeaks.ha"):
        ha = ha_with(refused)
        assert await ha.active_satellite() is None
        assert await ha.active_satellite() is None  # avertissement pas répété dans la minute
        t = time.perf_counter()
        assert await ha_with(slow).active_satellite(timeout=0.2) is None
        assert time.perf_counter() - t < 0.5
    warnings = [r for r in caplog.records if "Satellite actif inconnu" in r.message]
    assert len(warnings) == 2  # un par client (refusé, lent)


async def test_disabled_without_token():
    assert await HomeAssistant("", "", "s", "e").active_satellite() is None


# --- Proxy -----------------------------------------------------------------------
async def send(port, audio, context=None):
    async with AsyncTcpClient("127.0.0.1", port) as client:
        await client.write_event(Transcribe(language="fr", context=context).event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        await client.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=audio).event())
        t_stop = time.perf_counter()
        await client.write_event(AudioStop().event())
        while True:
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            if Transcript.is_type(event.type):
                return event, time.perf_counter() - t_stop


def proxy_with(service, uri, fake):
    ha = HomeAssistant("http://ha.test", "jeton", "sensor.jarvis_interlocuteur", "jarvis_interlocuteur")
    ha._client = httpx.AsyncClient(base_url="http://ha.test", transport=fake.transport())
    return Proxy(uri, service, ha), ha


async def test_satellite_from_ha(service, whisper):  # noqa: F811
    _, uri = whisper
    fake = FakeHA(SALON)
    proxy, ha = proxy_with(service, uri, fake)
    server, port = await start_proxy(proxy)
    await send(port, pcm(PIERRE))
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["satellite"] == "Salon" and row["satellite_entity"] == SALON["entity_id"]
    sensor = json.loads(next(b for _, p, b in fake.calls if p.startswith("/api/states")))
    assert sensor["attributes"]["satellite"] == "Salon"
    assert sensor["attributes"]["satellite_entity"] == SALON["entity_id"]
    event = json.loads(next(b for _, p, b in fake.calls if p.startswith("/api/events")))
    assert event["satellite"] == "Salon"
    server.close()
    await ha.close()


async def test_satellite_name_without_area(service, whisper):  # noqa: F811
    _, uri = whisper
    proxy, ha = proxy_with(service, uri, FakeHA({**SALON, "piece": ""}))
    server, port = await start_proxy(proxy)
    await send(port, pcm(PIERRE))
    await asyncio.sleep(0.3)
    assert service.list_phrases(1)[0]["satellite"] == "VBAN Satellite Salon"
    server.close()
    await ha.close()


async def test_context_has_priority(service, whisper, caplog):  # noqa: F811
    _, uri = whisper
    fake = FakeHA(SALON)
    proxy, ha = proxy_with(service, uri, fake)
    server, port = await start_proxy(proxy)
    with caplog.at_level(logging.INFO, logger="whospeaks.proxy"):
        await send(port, pcm(PIERRE), context={"satellite_id": "atom_bureau"})
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["satellite"] == "atom_bureau" and row["satellite_entity"] == "atom_bureau"
    assert fake.template_calls == 0
    assert any("satellite source: context" in r.message for r in caplog.records)
    server.close()
    await ha.close()


async def test_unknown_satellite(service, whisper, caplog):  # noqa: F811
    _, uri = whisper
    proxy, ha = proxy_with(service, uri, FakeHA(None))
    server, port = await start_proxy(proxy)
    with caplog.at_level(logging.INFO, logger="whospeaks.proxy"):
        await send(port, pcm(PIERRE))
    await asyncio.sleep(0.3)
    row = service.list_phrases(1)[0]
    assert row["satellite"] is None and row["satellite_entity"] is None
    assert any("satellite source: inconnu" in r.message for r in caplog.records)
    server.close()
    await ha.close()


async def test_slow_ha_does_not_delay_transcript(service, whisper):  # noqa: F811
    _, uri = whisper

    async def handler(request):
        if request.url.path == "/api/template":
            await asyncio.sleep(3)
        return httpx.Response(200, json={})
    ha = HomeAssistant("http://ha.test", "jeton", "sensor.x", "x")
    ha._client = httpx.AsyncClient(base_url="http://ha.test", transport=httpx.MockTransport(handler))
    server, port = await start_proxy(Proxy(uri, service, ha))
    event, delay = await send(port, pcm(PIERRE))
    assert event.data["text"] and delay < WHISPER_DELAY + 0.2
    await asyncio.sleep(1.3)  # le journal attend HA au plus 1 s
    assert service.list_phrases(1)[0]["satellite"] is None
    server.close()
    await ha.close()


async def test_reprise_does_not_cross_satellites(service, whisper):  # noqa: F811
    _, uri = whisper
    pid = service.create_person("Pierre")
    service.add_audio_samples(pid, voice(PIERRE, 8.0, seed=1), "fichier")
    fake = FakeHA(SALON)
    proxy, ha = proxy_with(service, uri, fake)
    server, port = await start_proxy(proxy)
    event, _ = await send(port, pcm(PIERRE, 2.0, seed=2))
    assert event.data["speaker"] == "Pierre"
    fake.satellite = BUREAU
    event, _ = await send(port, pcm(PIERRE, 0.6, seed=3))
    assert event.data["speaker"] == "trop court"  # pas de reprise depuis le salon
    fake.satellite = SALON
    event, _ = await send(port, pcm(PIERRE, 0.6, seed=4))
    assert event.data["speaker"] == "Pierre"  # reprise au salon
    server.close()
    await ha.close()


# --- Base et API -----------------------------------------------------------------
def test_migration_adds_satellite_entity(tmp_path):
    path = tmp_path / "whospeaks.db"
    old = sqlite3.connect(path)
    old.executescript(dbmod.SCHEMA.replace("    satellite_entity TEXT,\n", ""))
    old.execute("INSERT INTO phrases(horodatage, satellite, resultat) VALUES(1, 'Salon', 'inconnu')")
    old.commit()
    assert "satellite_entity" not in {r[1] for r in old.execute("PRAGMA table_info(phrases)")}
    old.close()
    db = dbmod.Database(path)
    assert "satellite_entity" in {r[1] for r in db.all("PRAGMA table_info(phrases)")}
    assert db.one("SELECT satellite, satellite_entity FROM phrases")["satellite"] == "Salon"
    db.close()
    dbmod.Database(path).close()  # deuxième ouverture : rien à migrer


def test_api_satellite_filter_stats_and_bulk(service):
    for satellite, entity in (("Salon", SALON["entity_id"]), ("Bureau", BUREAU["entity_id"]), (None, None)):
        service.record(service.analyze(pcm(PIERRE, seed=1)), "allume", satellite, None, entity)
        service.record(service.analyze(silence()), None, satellite, None, entity)
    service.record(service.analyze(silence(seed=5)), None, "Salon", None, SALON["entity_id"])
    client = TestClient(create_app(service, service.config))
    salon = client.get("/api/phrases?satellite=Salon").json()
    assert len(salon) == 3 and {p["satellite"] for p in salon} == {"Salon"}
    assert salon[0]["satellite_entity"] == SALON["entity_id"]
    assert [p["satellite"] for p in client.get("/api/phrases?satellite=%3F").json()] == [None, None]
    assert len(client.get("/api/phrases?filtre=ignoree_silence&satellite=Salon").json()) == 2
    stats = {s["satellite"]: s for s in client.get("/api/satellites").json()}
    assert stats["Salon"] == {"satellite": "Salon", "total_24h": 3, "ignorees_24h": 2}
    assert stats[None]["ignorees_24h"] == 1
    assert list(stats) == ["Bureau", "Salon", None]
    # Suppression groupée limitée au satellite filtré
    assert client.get("/api/phrases/compte?filtre=ignoree_silence&satellite=Salon").json()["nombre"] == 2
    assert client.delete("/api/phrases?filtre=ignoree_silence&satellite=Salon").json()["supprimees"] == 2
    assert service.count_phrases("ignoree_silence") == 2  # Bureau et inconnu restent


def test_old_phrases_out_of_24h_window(service):
    a = service.analyze(silence())
    a.horodatage = time.time() - 2 * 86400
    service.record(a, None, "Chambre")
    stats = {s["satellite"]: s for s in service.satellite_stats()}
    assert stats["Chambre"] == {"satellite": "Chambre", "total_24h": 0, "ignorees_24h": 0}
