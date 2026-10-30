"""Configuration : variables d'environnement (infrastructure) et réglages par défaut.

Les réglages modifiables depuis la page (seuils, rétention…) sont stockés en base ;
les valeurs ci-dessous ne servent que de valeurs initiales.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from .filters import check_regexes

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(value: str) -> list[IPNetwork]:
    """« 10.0.0.1, 172.18.0.0/16 fd00::/8 » -> réseaux ; une IP seule devient un /32 (/128)."""
    networks = []
    for item in value.replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as err:
            raise ValueError(f"TRUSTED_PROXIES : entrée invalide « {item} »") from err
    return networks


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "oui", "on")


@dataclass
class Config:
    upstream_uri: str = "tcp://wyoming-whisper-gpu:10300"
    wyoming_uri: str = "tcp://0.0.0.0:10302"
    web_host: str = "0.0.0.0"
    web_port: int = 8095
    ha_url: str = ""
    ha_token: str = ""
    ui_token: str = ""
    ui_local_only: bool = True
    # Origines autorisées à afficher la page dans une iframe (en plus de HA_URL).
    frame_ancestors: list[str] = field(default_factory=list)
    # Proxys inverses dont on croit les en-têtes X-Forwarded-For / X-Real-IP / X-Forwarded-Proto.
    trusted_proxies: list[IPNetwork] = field(default_factory=list)
    model: str = "campplus-zh-en"
    lang: str = "fr"
    data_dir: Path = Path("/data")
    num_threads: int = 4
    sensor_entity: str = "sensor.jarvis_interlocuteur"
    ha_event: str = "jarvis_interlocuteur"
    # Délais (secondes)
    ha_timeout: float = 0.3
    id_extra_wait: float = 0.15
    upstream_connect_timeout: float = 5.0
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "whospeaks.db"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        frame = [o.strip() for o in env("FRAME_ANCESTORS", "").split() if o.strip()]
        return cls(
            upstream_uri=env("UPSTREAM_URI", cls.upstream_uri),
            wyoming_uri=env("WYOMING_URI", cls.wyoming_uri),
            web_host=env("WEB_HOST", cls.web_host),
            web_port=int(env("WEB_PORT", str(cls.web_port))),
            ha_url=env("HA_URL", "").rstrip("/"),
            ha_token=env("HA_TOKEN", ""),
            ui_token=env("UI_TOKEN", ""),
            ui_local_only=_env_bool("UI_LOCAL_ONLY", True),
            frame_ancestors=frame,
            trusted_proxies=parse_networks(env("TRUSTED_PROXIES", "")),
            model=env("MODEL", cls.model),
            lang=env("LANG_STT", env("WHOSPEAKS_LANG", _lang_from_env())),
            data_dir=Path(env("DATA_DIR", "/data")),
            num_threads=int(env("NUM_THREADS", str(cls.num_threads))),
            sensor_entity=env("HA_SENSOR", cls.sensor_entity),
            ha_event=env("HA_EVENT", cls.ha_event),
            ha_timeout=float(env("HA_TIMEOUT", str(cls.ha_timeout))),
            id_extra_wait=float(env("ID_EXTRA_WAIT", str(cls.id_extra_wait))),
            log_level=env("LOG_LEVEL", cls.log_level),
        )


def _lang_from_env() -> str:
    # LANG=fr est demandé dans le cahier des charges ; on ignore les locales POSIX (fr_FR.UTF-8, C.UTF-8).
    value = os.environ.get("LANG", "fr")
    if not value or value.startswith("C.") or value in ("C", "POSIX"):
        return "fr"
    return value.split("_")[0].split(".")[0] or "fr"


# Réglages modifiables depuis la page (stockés dans la table `reglages`).
DEFAULT_SETTINGS: dict = {
    "seuil_identification": 0.55,
    "marge_min": 0.08,
    "agregation": "max",  # "max" ou "top3"
    "duree_min_parole": 1.0,
    "duree_min_enrolement": 1.5,
    "seuil_cluster": 0.60,
    "seuil_ignore": 0.60,
    "enrichissement_auto": True,
    "seuil_auto": 0.75,
    "marge_auto": 0.15,
    "max_echantillons": 200,
    "retention_jours": 30,
    "garder_audio": True,
    # Garde l'audio des échantillons au-delà de la rétention : permet de recalculer
    # les empreintes si l'on change de modèle.
    "garder_audio_echantillons": True,
    # Phrase trop courte : reprendre l'interlocuteur identifié il y a moins de N secondes.
    "reprise_active": True,
    "reprise_duree_s": 120,
    # Pas de parole (VAD) ou texte inventé par Whisper : transcript vide, HA ne répond pas.
    "filtre_silence_actif": True,
    "silence_max_s": 0.3,
    # Comparées au texte normalisé ; « ~ » en tête : expression régulière.
    "hallucinations": [
        "merci",
        "sous-titrage st' 501",
        "sous-titres réalisés par la communauté d'amara.org",
        "...",
    ],
    "modele": None,  # None : on utilise la variable MODEL
    "personnes_ha": {},  # nom -> person.xxx (complète le champ de chaque personne)
}

# Bornes des réglages numériques (min, max) ; les autres réglages ne sont contrôlés qu'en type.
_BOUNDS: dict[str, tuple[float, float]] = {
    "reprise_duree_s": (0, 600),
    "silence_max_s": (0, 2),
}


def validate_settings(values: dict) -> dict:
    """Vérifie type et bornes des réglages envoyés par la page ; lève ValueError."""
    clean = {}
    for key, value in values.items():
        default = DEFAULT_SETTINGS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} : vrai ou faux attendu")
        elif isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} : nombre attendu")
            low, high = _BOUNDS.get(key, (float("-inf"), float("inf")))
            if not low <= value <= high:
                raise ValueError(f"{key} : doit être entre {low:g} et {high:g}")
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} : liste de textes attendue")
            value = [v.strip() for v in value if v.strip()]
            if key == "hallucinations":
                check_regexes(value)
        clean[key] = value
    return clean
