"""Envoi du capteur et des événements à Home Assistant (API REST)."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

_LOGGER = logging.getLogger(__name__)

# Satellite qui écoute en ce moment (le plus récemment passé en écoute s'il y en a plusieurs).
# HA ne dit pas à WhoSpeaks quel satellite parle : on lui demande.
SATELLITE_TEMPLATE = """\
{%- set s = states.assist_satellite
      | selectattr('state', 'in', ['listening', 'processing'])
      | sort(attribute='last_changed', reverse=true) | list -%}
{%- if s -%}
{{ {'entity_id': s[0].entity_id, 'nom': s[0].name, 'piece': area_name(s[0].entity_id) or ''} | to_json }}
{%- endif -%}"""
SATELLITE_TIMEOUT = 1.0
_WARN_EVERY = 60.0


class HomeAssistant:
    def __init__(self, url: str, token: str, sensor: str, event: str, timeout: float = 0.3) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.sensor = sensor
        self.event = event
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.last_error: str | None = None
        self._satellite_warned_at = float("-inf")

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

    async def active_satellite(self, timeout: float = SATELLITE_TIMEOUT) -> dict | None:
        """Satellite en écoute : {entity_id, nom, piece}, ou None. Ne lève jamais d'exception."""
        if not self.enabled:
            return None
        try:
            resp = await asyncio.wait_for(
                self._http().post("/api/template", json={"template": SATELLITE_TEMPLATE}), timeout
            )
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                return None
            data = json.loads(text)
            if not isinstance(data, dict) or not data.get("entity_id"):
                return None
            return {"entity_id": str(data["entity_id"]), "nom": str(data.get("nom") or ""),
                    "piece": str(data.get("piece") or "")}
        except Exception as err:  # noqa: BLE001
            now = time.monotonic()
            if now - self._satellite_warned_at >= _WARN_EVERY:
                self._satellite_warned_at = now
                _LOGGER.warning("Satellite actif inconnu (HA) : %s: %s", type(err).__name__, err)
            return None

    async def publish_timings(self, temps: dict, satellite: str | None, resultat: str | None) -> None:
        """sensor.whospeaks_temps_whisper et sensor.whospeaks_temps_total (secondes).

        Publié après la réponse à HA, sans la retarder ; ne lève jamais d'exception.
        """
        attributes = {
            "unit_of_measurement": "s", "device_class": "duration", "state_class": "measurement",
            "satellite": satellite, "resultat": resultat,
            **{k: None if v is None else round(v, 3) for k, v in temps.items()},
        }
        posts = []
        for key, entity, label in (("whisper_s", "sensor.whospeaks_temps_whisper", "Temps Whisper"),
                                   ("total_s", "sensor.whospeaks_temps_total", "Temps total de la phrase")):
            if temps.get(key) is not None:
                body = {"state": round(temps[key], 2),
                        "attributes": {**attributes, "friendly_name": label, "icon": "mdi:timer-outline"}}
                posts.append(self._post(f"/api/states/{entity}", body, timeout=2.0))
        if not posts:
            return
        try:
            await asyncio.gather(*posts)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Capteurs de temps non publiés : %s", err)

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
