"""Envoi du capteur et des événements à Home Assistant (API REST)."""

from __future__ import annotations

import asyncio
import logging

import httpx

_LOGGER = logging.getLogger(__name__)


class HomeAssistant:
    def __init__(self, url: str, token: str, sensor: str, event: str, timeout: float = 0.3) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.sensor = sensor
        self.event = event
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.url,
                headers={"Authorization": f"Bearer {self.token}"},
                # Connexions gardées ouvertes : évite la poignée de main TCP à chaque phrase.
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300),
                timeout=httpx.Timeout(5.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def publish(self, payload: dict, timeout: float | None = None) -> bool:
        """Capteur + événement en parallèle ; ne lève jamais d'exception."""
        if not self.enabled:
            return False
        event_data = {"interlocuteur": payload["state"], **payload["attributes"]}
        event_data.pop("friendly_name", None)
        event_data.pop("icon", None)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._post(f"/api/states/{self.sensor}", payload),
                    self._post(f"/api/events/{self.event}", event_data),
                ),
                timeout=timeout if timeout is not None else self.timeout,
            )
            self.last_error = None
            return True
        except Exception as err:  # noqa: BLE001
            self.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning("Home Assistant injoignable : %s", self.last_error)
            return False

    async def fire(self, event_type: str, data: dict) -> None:
        if not self.enabled:
            return
        try:
            await self._post(f"/api/events/{event_type}", data, timeout=2.0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Événement %s non envoyé : %s", event_type, err)

    async def _post(self, path: str, body: dict, timeout: float | None = None) -> None:
        resp = await self._http().post(path, json=body, timeout=timeout)
        resp.raise_for_status()
