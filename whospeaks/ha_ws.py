"""Lecture du vocabulaire de la maison par l'API WebSocket de Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging

from websockets.asyncio.client import connect

from .vocab import parse_ha

_LOGGER = logging.getLogger(__name__)

# Commandes WebSocket et valeur de repli si HA ne les connaît pas (étages : HA récent)
_COMMANDS = {
    "exposed": ("homeassistant/expose_entity/list", {}),
    "registry": ("config/entity_registry/list", []),
    "states": ("get_states", []),
    "areas": ("config/area_registry/list", []),
    "floors": ("config/floor_registry/list", []),
}


class HaWsError(Exception):
    pass


VOCAB_REFRESH = 15 * 60  # secondes entre deux relevés du vocabulaire


async def refresh_vocabulary(service, http_url: str, token: str) -> bool:
    """Relève le vocabulaire dans HA et le donne au service ; garde l'ancien en cas d'échec."""
    if not (http_url and token):
        service.vocab_ha_erreur = "HA_URL ou HA_TOKEN absent"
        return False
    try:
        data = await fetch_vocabulary(http_url, token)
    except HaWsError as err:
        service.vocab_ha_erreur = str(err)
        _LOGGER.warning("Vocabulaire HA non relevé : %s", err)
        return False
    service.set_ha_vocabulary(data)
    _LOGGER.info("Vocabulaire HA : %d entités partagées, %d pièces, %d étages ; prompt de %d termes",
                 len(data["entities"]), len(data["areas"]), len(data["floors"]), len(service.prompt_terms))
    return True


async def vocabulary_loop(service, http_url: str, token: str) -> None:
    """Au démarrage, puis toutes les 15 minutes."""
    while True:
        await refresh_vocabulary(service, http_url, token)
        await asyncio.sleep(VOCAB_REFRESH)


def ws_url(http_url: str) -> str:
    url = http_url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return url + "/api/websocket"


async def fetch_vocabulary(http_url: str, token: str, timeout: float = 15.0) -> dict:
    """Entités partagées avec Assist (noms, alias), pièces et étages. Lève HaWsError."""
    try:
        return await asyncio.wait_for(_fetch(ws_url(http_url), token), timeout)
    except HaWsError:
        raise
    except Exception as err:  # noqa: BLE001
        raise HaWsError(f"{type(err).__name__}: {err}") from err


async def _fetch(url: str, token: str) -> dict:
    async with connect(url, max_size=64 * 1024 * 1024, open_timeout=10) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise HaWsError(f"réponse inattendue : {hello.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            raise HaWsError("jeton refusé par Home Assistant")
        ids = {}
        for n, (key, (command, _)) in enumerate(_COMMANDS.items(), start=1):
            ids[n] = key
            await ws.send(json.dumps({"id": n, "type": command}))
        results: dict = {}
        while len(results) < len(ids):
            msg = json.loads(await ws.recv())
            key = ids.get(msg.get("id"))
            if key is None or msg.get("type") != "result":
                continue
            if msg.get("success"):
                results[key] = msg.get("result")
            else:
                if key in ("exposed", "registry"):
                    raise HaWsError(f"{_COMMANDS[key][0]} : {(msg.get('error') or {}).get('message')}")
                _LOGGER.debug("%s indisponible : %s", _COMMANDS[key][0], msg.get("error"))
                results[key] = _COMMANDS[key][1]
    return parse_ha(results["exposed"] or {}, results["registry"] or [], results["states"] or [],
                    results["areas"] or [], results["floors"] or [])
