"""Point d'entrée : proxy Wyoming (10302) + page web (8095) dans la même boucle asyncio."""

from __future__ import annotations

import asyncio
import logging
from functools import partial

import uvicorn
from wyoming.server import AsyncServer

from .config import Config
from .ha import HomeAssistant
from .proxy import Proxy, ProxyHandler
from .service import WhoSpeaks
from .web import create_app

_LOGGER = logging.getLogger("whospeaks")

_MODEL_RETRY = 300  # secondes entre deux tentatives de chargement du modèle
_RETENTION_PERIOD = 3600


async def _maintenance(service: WhoSpeaks) -> None:
    """Réessaie de charger le modèle s'il manque, et applique la rétention de l'audio."""
    loop = asyncio.get_running_loop()
    last_retention = 0.0
    while True:
        if not service.ready:
            await loop.run_in_executor(None, service.load_models)
        if loop.time() - last_retention > _RETENTION_PERIOD:
            last_retention = loop.time()
            try:
                await loop.run_in_executor(None, service.purge_clips)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Rétention en échec")
        await asyncio.sleep(_MODEL_RETRY if not service.ready else 600)


async def _run(config: Config) -> None:
    ha = HomeAssistant(config.ha_url, config.ha_token, config.sensor_entity, config.ha_event,
                       config.ha_timeout)
    if not ha.enabled:
        _LOGGER.warning("HA_URL ou HA_TOKEN absent : pas de notification Home Assistant")

    service: WhoSpeaks | None
    try:
        service = WhoSpeaks(config)
        service.loop = asyncio.get_running_loop()
        service.event_callback = ha.fire
    except Exception:  # noqa: BLE001
        # Sans base utilisable, le proxy reste transparent.
        _LOGGER.exception("Service d'identification indisponible, proxy en mode transparent")
        service = None

    proxy = Proxy(config.upstream_uri, service, ha, config.lang, config.id_extra_wait,
                  config.upstream_connect_timeout)
    server = AsyncServer.from_uri(config.wyoming_uri)
    _LOGGER.info("Proxy Wyoming sur %s → %s", config.wyoming_uri, config.upstream_uri)

    tasks = [asyncio.create_task(server.run(partial(ProxyHandler, proxy=proxy)), name="wyoming")]
    if service is not None:
        tasks.append(asyncio.create_task(_maintenance(service), name="maintenance"))
        app = create_app(service, config)
        web = uvicorn.Server(uvicorn.Config(app, host=config.web_host, port=config.web_port,
                                            log_level="warning", access_log=False))
        tasks.append(asyncio.create_task(web.serve(), name="web"))
        _LOGGER.info("Page web sur http://%s:%d", config.web_host, config.web_port)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception():
                raise task.exception()
    finally:
        for task in tasks:
            task.cancel()
        await ha.close()


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.ui_token:
        _LOGGER.warning("UI_TOKEN absent : la page web n'est pas protégée par un jeton")
    asyncio.run(_run(config))


if __name__ == "__main__":
    main()
