"""Cœur de WhoSpeaks : identification, voix inconnues, enrôlement, amélioration continue."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from . import calibration, clustering, rotation
from .audio import SAMPLE_RATE, pcm16_to_float, read_wav, write_wav
from .config import DEFAULT_SETTINGS, Config, validate_settings
from .db import Database, from_blob, to_blob
from .engine import Embedder, Vad
from .matching import identify
from .models import VAD_MODEL, ensure_model, resolve_speaker_model

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[str, dict], Awaitable[None]]

# Longueur des morceaux quand un fichier d'enrôlement est long.
_CHUNK_SECONDS = 4.0
# Au-delà, la parole n'améliore plus l'empreinte mais allonge le calcul.
_MAX_EMBED_SECONDS = 10.0


class ServiceError(Exception):
    """Erreur métier renvoyée telle quelle à la page."""


@dataclass
class Analysis:
    resultat: str  # identifie | inconnu | trop_court | enrolement | reprise | erreur
    duree_parole: float = 0.0
    person_id: int | None = None
    nom: str | None = None
    person_entity: str | None = None
    score: float | None = None
    marge: float | None = None
    cluster_id: int | None = None
    ignoree: bool = False
    temps_empreinte_ms: float = 0.0
    erreur: str | None = None
    # Âge (s) de l'identification reprise, pour le résultat « reprise ».
    reprise_age: float | None = None
    horodatage: float = field(default_factory=time.time)
    audio: np.ndarray | None = field(default=None, repr=False)
    emb: np.ndarray | None = field(default=None, repr=False)

    @property
    def etat(self) -> str:
        if self.resultat in ("identifie", "enrolement", "reprise") and self.nom:
            return self.nom
        if self.resultat == "trop_court":
            return "trop court"
        return "inconnu"

    def public(self) -> dict:
        """Résultat exposé (cahier des charges §4.2)."""
        return {
            "person_id": self.person_id,
            "nom": self.nom,
            "person_entity": self.person_entity,
            "score": None if self.score is None else round(self.score, 4),
            "marge": None if self.marge is None else round(self.marge, 4),
            "cluster_id": self.cluster_id,
            "duree_parole": round(self.duree_parole, 2),
            "resultat": self.resultat,
        }


@dataclass
class Memo:
    """Dernière identification sûre (identifie ou enrolement), pour la reprise."""

    person_id: int
    nom: str | None
    person_entity: str | None
    score: float | None
    horodatage: float
    satellite: str | None


@dataclass
class Enrolement:
    personne_id: int
    nom: str
    total: int
    n: int = 0
    expire: float = 0.0


def _now_iso(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="seconds")


class WhoSpeaks:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.settings = self.db.load_settings(DEFAULT_SETTINGS)
        self.vad: Vad | None = None
        self.embedder: Embedder | None = None
        self.model_error: str | None = None
        self._lock = threading.RLock()
        self._model_lock = threading.Lock()
        self._persons: dict[int, np.ndarray] = {}
        self._person_meta: dict[int, dict] = {}
        self._cluster_ids: list[int] = []
        self._cluster_cents = np.zeros((0, 0), dtype=np.float32)
        self._cluster_sizes: list[int] = []
        self._ignores = np.zeros((0, 0), dtype=np.float32)
        self.enrolement: Enrolement | None = None
        # Dernière identification par satellite ; la clé None est l'entrée globale.
        self._memory: dict[str | None, Memo] = {}
        self.task: dict[str, Any] = {"nom": None, "etat": "inactif", "progression": 0.0, "message": ""}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.event_callback: EventCallback | None = None

    # ------------------------------------------------------------------ modèles
    @property
    def model_key(self) -> str:
        return self.settings.get("modele") or self.config.model

    @property
    def ready(self) -> bool:
        return self.embedder is not None

    def load_models(self) -> None:
        """Charge VAD et modèle d'empreinte ; en cas d'échec, le proxy reste transparent."""
        with self._model_lock:
            if self.vad is None:
                try:
                    self.vad = Vad(ensure_model(VAD_MODEL, self.config.models_dir))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("VAD Silero indisponible (%s), repli sur l'énergie", err)
                    self.vad = Vad(None)
            key = self.model_key
            if self.embedder is not None and self.embedder.key == key:
                return
            try:
                info = resolve_speaker_model(key)
                path = ensure_model(info, self.config.models_dir)
                self.embedder = Embedder(path, info.key, self.config.num_threads)
                self.model_error = None
                _LOGGER.info("Modèle d'empreinte chargé : %s (%d dim)", info.key, self.embedder.dim)
            except Exception as err:  # noqa: BLE001
                self.model_error = str(err)
                _LOGGER.error("Modèle d'empreinte indisponible (%s) : %s", key, err)
                return
        self.reload()

    def reload(self) -> None:
        """Recharge les caches mémoire depuis la base."""
        model = self.embedder.key if self.embedder else self.model_key
        with self._lock:
            meta = {
                r["id"]: dict(r)
                for r in self.db.all("SELECT id, nom, person_entity FROM personnes ORDER BY nom")
            }
            mapping = self.settings.get("personnes_ha") or {}
            for m in meta.values():
                if not m.get("person_entity"):
                    m["person_entity"] = mapping.get(m["nom"])
            persons: dict[int, list[np.ndarray]] = {}
            for r in self.db.all(
                "SELECT personne_id, empreinte FROM echantillons WHERE modele=?", (model,)
            ):
                persons.setdefault(r["personne_id"], []).append(from_blob(r["empreinte"]))
            self._person_meta = meta
            self._persons = {pid: np.stack(v) for pid, v in persons.items() if pid in meta}

            rows = self.db.all(
                "SELECT id, centroide, taille FROM clusters WHERE modele=? AND centroide IS NOT NULL",
                (model,),
            )
            self._cluster_ids = [r["id"] for r in rows]
            self._cluster_sizes = [r["taille"] for r in rows]
            self._cluster_cents = (
                np.stack([from_blob(r["centroide"]) for r in rows])
                if rows
                else np.zeros((0, 0), dtype=np.float32)
            )
            ign = self.db.all("SELECT empreinte FROM ignores WHERE modele=?", (model,))
            self._ignores = (
                np.stack([from_blob(r["empreinte"]) for r in ign])
                if ign
                else np.zeros((0, 0), dtype=np.float32)
            )

    def update_settings(self, values: dict) -> dict:
        try:
            clean = validate_settings({k: v for k, v in values.items() if k in DEFAULT_SETTINGS})
        except ValueError as err:
            raise ServiceError(str(err)) from err
        old_model = self.model_key
        self.db.save_settings(clean)
        self.settings.update(clean)
        if self.model_key != old_model:
            self.change_model_async()
        else:
            self.reload()
        return self.settings

    # ------------------------------------------------------------ identification
    def analyze(self, pcm: bytes, satellite: str | None = None) -> Analysis:
        """Appelé dans un thread à la fin de chaque phrase (audio 16 kHz mono 16 bits)."""
        audio = pcm16_to_float(pcm)
        if self.embedder is None or self.vad is None:
            return Analysis("erreur", audio=audio, erreur=self.model_error or "modèle non chargé")
        s = self.settings
        t0 = time.perf_counter()
        speech = self.vad.speech(audio)
        duree = len(speech) / SAMPLE_RATE
        enrol = self._active_enrolement()
        if duree < s["duree_min_parole"]:
            # Pendant l'enrôlement aussi : une phrase trop courte n'est pas comptée.
            result = self._reprise(satellite) or Analysis("trop_court")
            result.duree_parole = duree
            result.audio = audio
            result.temps_empreinte_ms = (time.perf_counter() - t0) * 1000
            return result
        if enrol is not None and duree < s["duree_min_enrolement"]:
            enrol = None
        emb = self.embedder.embed(speech[: int(_MAX_EMBED_SECONDS * SAMPLE_RATE)])
        t_emb = (time.perf_counter() - t0) * 1000

        with self._lock:
            match = identify(
                emb, self._persons, s["seuil_identification"], s["marge_min"], s["agregation"]
            )
            result = Analysis(
                "inconnu", duree_parole=duree, score=match.score, marge=match.marge,
                temps_empreinte_ms=t_emb, audio=audio, emb=emb,
            )
            if enrol is not None:
                result.resultat = "enrolement"
                self._fill_person(result, enrol.personne_id)
            elif match.person_id is not None:
                result.resultat = "identifie"
                self._fill_person(result, match.person_id)
            elif self._is_ignored(emb):
                result.ignoree = True
            else:
                result.cluster_id = self._assign_cluster(emb)
            if result.person_id is not None:
                self._remember(result, satellite)
        return result

    def _remember(self, result: Analysis, satellite: str | None) -> None:
        """Mémorise une identification sûre ; appelé sous self._lock."""
        memo = Memo(result.person_id, result.nom, result.person_entity, result.score,
                    result.horodatage, satellite)
        self._memory[None] = memo
        if satellite:
            self._memory[satellite] = memo

    def _reprise(self, satellite: str | None) -> Analysis | None:
        """Phrase trop courte : reprend l'interlocuteur identifié récemment.

        Clé : le satellite s'il est connu (pas de reprise croisée entre satellites),
        sinon l'entrée globale. La mémoire n'est pas rafraîchie, pour qu'une suite
        de phrases courtes ne prolonge pas la reprise indéfiniment.
        """
        s = self.settings
        if not s["reprise_active"]:
            return None
        with self._lock:
            memo = self._memory.get(satellite or None)
            # Nom et entité à jour ; personne supprimée entre-temps : pas de reprise.
            meta = self._person_meta.get(memo.person_id) if memo else None
        if memo is None or meta is None:
            return None
        age = time.time() - memo.horodatage
        if age > float(s["reprise_duree_s"]):
            return None
        return Analysis("reprise", person_id=memo.person_id, nom=meta.get("nom"),
                        person_entity=meta.get("person_entity"), score=memo.score, reprise_age=age)

    def _fill_person(self, result: Analysis, person_id: int) -> None:
        meta = self._person_meta.get(person_id, {})
        result.person_id = person_id
        result.nom = meta.get("nom")
        result.person_entity = meta.get("person_entity")

    def _is_ignored(self, emb: np.ndarray) -> bool:
        if self._ignores.size == 0:
            return False
        return float(np.max(self._ignores @ emb)) >= self.settings["seuil_ignore"]

    def _assign_cluster(self, emb: np.ndarray) -> int:
        """Regroupement « leader » incrémental ; appelé sous self._lock."""
        now = time.time()
        idx, sim = clustering.nearest(emb, self._cluster_cents)
        if idx >= 0 and sim >= self.settings["seuil_cluster"]:
            cid = self._cluster_ids[idx]
            size = self._cluster_sizes[idx]
            cent = clustering.update_centroid(self._cluster_cents[idx], size, emb)
            self._cluster_cents[idx] = cent
            self._cluster_sizes[idx] = size + 1
            self.db.execute(
                "UPDATE clusters SET centroide=?, taille=taille+1, derniere_apparition=? WHERE id=?",
                (to_blob(cent), now, cid),
            )
            return cid
        cur = self.db.execute(
            "INSERT INTO clusters(centroide, taille, modele, cree_le, derniere_apparition) "
            "VALUES(?, 1, ?, ?, ?)",
            (to_blob(emb), self.embedder.key, now, now),
        )
        cid = int(cur.lastrowid)
        self._cluster_ids.append(cid)
        self._cluster_sizes.append(1)
        self._cluster_cents = (
            emb[None, :].copy()
            if self._cluster_cents.size == 0
            else np.vstack([self._cluster_cents, emb[None, :]])
        )
        return cid

    def record(
        self,
        analysis: Analysis,
        transcript: str | None,
        satellite: str | None = None,
        temps_ajoute_ms: float | None = None,
    ) -> int:
        """Enregistre la phrase dans le journal (après le renvoi du transcript)."""
        s = self.settings
        etat = "ignoree" if analysis.ignoree else "nouvelle"
        if analysis.resultat in ("identifie", "enrolement"):
            etat = "attribuee"
        model = self.embedder.key if self.embedder else None
        cur = self.db.execute(
            "INSERT INTO phrases(horodatage, satellite, transcript, resultat, personne_id, score, "
            "marge, cluster_id, duree_parole, empreinte, modele, etat, temps_empreinte_ms, "
            "temps_ajoute_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                analysis.horodatage, satellite, transcript, analysis.resultat, analysis.person_id,
                analysis.score, analysis.marge, analysis.cluster_id, analysis.duree_parole,
                None if analysis.emb is None else to_blob(analysis.emb),
                model if analysis.emb is not None else None, etat,
                analysis.temps_empreinte_ms, temps_ajoute_ms,
            ),
        )
        phrase_id = int(cur.lastrowid)
        clip = None
        if s["garder_audio"] and analysis.audio is not None and len(analysis.audio):
            clip = self._clip_path(phrase_id, analysis.horodatage)
            write_wav(self.config.data_dir / clip, analysis.audio)
            self.db.execute("UPDATE phrases SET clip=? WHERE id=?", (clip, phrase_id))

        # Seules ces deux issues ont une empreinte fiable ; une « reprise » n'en a pas.
        if analysis.emb is not None and analysis.person_id is not None:
            if analysis.resultat == "enrolement":
                self._add_sample(analysis.person_id, analysis.emb, clip, transcript,
                                 "enrolement", phrase_id, analysis.duree_parole)
                self._enrolement_progress()
            elif (
                analysis.resultat == "identifie"
                and s["enrichissement_auto"]
                and (analysis.score or 0) >= s["seuil_auto"]
                and (analysis.marge or 0) >= s["marge_auto"]
            ):
                self._add_sample(analysis.person_id, analysis.emb, clip, transcript,
                                 "auto", phrase_id, analysis.duree_parole)
        return phrase_id

    def _clip_path(self, phrase_id: int, ts: float) -> str:
        day = dt.datetime.fromtimestamp(ts)
        return f"clips/{day:%Y/%m/%d}/{phrase_id}.wav"

    def _add_sample(
        self, person_id: int, emb: np.ndarray, clip: str | None, transcript: str | None,
        source: str, phrase_id: int | None, duree: float | None, reload: bool = True,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO echantillons(personne_id, empreinte, modele, clip, transcript, source, "
            "phrase_id, duree, cree_le) VALUES(?,?,?,?,?,?,?,?,?)",
            (person_id, to_blob(emb), self.embedder.key, clip, transcript, source, phrase_id,
             duree, time.time()),
        )
        self._enforce_max(person_id)
        if reload:
            self.reload()
        return int(cur.lastrowid)

    def _enforce_max(self, person_id: int) -> None:
        rows = self.db.all(
            "SELECT id, empreinte, source, clip FROM echantillons WHERE personne_id=? AND modele=? "
            "ORDER BY id",
            (person_id, self.embedder.key),
        )
        max_count = int(self.settings["max_echantillons"])
        if len(rows) <= max_count:
            return
        embs = np.stack([from_blob(r["empreinte"]) for r in rows])
        # On retire en priorité les échantillons automatiques.
        removable = np.array([r["source"] == "auto" for r in rows])
        for idx in rotation.to_remove(embs, max_count, removable):
            self._delete_sample_row(rows[idx]["id"], rows[idx]["clip"])

    def _delete_sample_row(self, sample_id: int, clip: str | None) -> None:
        self.db.execute("DELETE FROM echantillons WHERE id=?", (sample_id,))
        self._delete_clip_if_unused(clip)

    def _delete_clip_if_unused(self, clip: str | None) -> None:
        if not clip:
            return
        used = self.db.one(
            "SELECT (SELECT COUNT(*) FROM echantillons WHERE clip=?) + "
            "(SELECT COUNT(*) FROM phrases WHERE clip=?) AS n",
            (clip, clip),
        )["n"]
        if not used:
            (self.config.data_dir / clip).unlink(missing_ok=True)

    def ha_payload(self, analysis: Analysis, transcript: str | None) -> dict:
        cluster = None
        if analysis.cluster_id is not None:
            cluster = self.cluster_name(analysis.cluster_id)
        return {
            "state": analysis.etat,
            "attributes": {
                "friendly_name": "Interlocuteur de Jarvis",
                "icon": "mdi:account-voice",
                "person_entity": analysis.person_entity,
                "score": None if analysis.score is None else round(analysis.score, 3),
                "marge": None if analysis.marge is None else round(analysis.marge, 3),
                "cluster": cluster,
                "cluster_id": analysis.cluster_id,
                "duree_parole": round(analysis.duree_parole, 2),
                "resultat": analysis.resultat,
                "reprise": analysis.resultat == "reprise",
                "age_s": None if analysis.reprise_age is None else round(analysis.reprise_age, 1),
                "transcript": transcript,
                "horodatage": _now_iso(analysis.horodatage),
            },
        }

    def cluster_name(self, cluster_id: int) -> str:
        row = self.db.one("SELECT nom FROM clusters WHERE id=?", (cluster_id,))
        if row and row["nom"]:
            return row["nom"]
        return f"Voix inconnue {cluster_id}"

    # ---------------------------------------------------------------- enrôlement
    def _active_enrolement(self) -> Enrolement | None:
        enrol = self.enrolement
        if enrol and enrol.expire and time.time() > enrol.expire:
            _LOGGER.info("Enrôlement de %s expiré (%d/%d)", enrol.nom, enrol.n, enrol.total)
            self.enrolement = None
            self.emit("whospeaks_enrolement",
                      {"personne": enrol.nom, "n": enrol.n, "total": enrol.total, "fin": "expire"})
            return None
        return enrol

    def start_enrolement(self, person_id: int, total: int = 12, duree_max: float = 900) -> Enrolement:
        meta = self._person(person_id)
        self.enrolement = Enrolement(
            person_id, meta["nom"], max(1, int(total)), 0, time.time() + duree_max if duree_max else 0
        )
        _LOGGER.info("Enrôlement de %s lancé (%d phrases)", meta["nom"], total)
        self.emit("whospeaks_enrolement", {"personne": meta["nom"], "n": 0, "total": total})
        return self.enrolement

    def stop_enrolement(self) -> None:
        enrol = self.enrolement
        self.enrolement = None
        if enrol:
            self.emit("whospeaks_enrolement",
                      {"personne": enrol.nom, "n": enrol.n, "total": enrol.total, "fin": "arret"})

    def _enrolement_progress(self) -> None:
        enrol = self.enrolement
        if enrol is None:
            return
        enrol.n += 1
        data = {"personne": enrol.nom, "n": enrol.n, "total": enrol.total}
        if enrol.n >= enrol.total:
            data["fin"] = "termine"
            self.enrolement = None
            _LOGGER.info("Enrôlement de %s terminé", enrol.nom)
        self.emit("whospeaks_enrolement", data)

    def emit(self, event_type: str, data: dict) -> None:
        """Envoie un événement à HA depuis n'importe quel thread, sans attendre."""
        if self.loop is None or self.event_callback is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.event_callback(event_type, data), self.loop)
        except RuntimeError:
            pass

    def add_audio_samples(
        self, person_id: int, audio: np.ndarray, source: str, transcript: str | None = None
    ) -> int:
        """Ajoute un fichier ou un enregistrement du navigateur comme échantillon(s)."""
        self._require_model()
        self._person(person_id)
        speech = self.vad.speech(audio)
        min_len = int(self.settings["duree_min_enrolement"] * SAMPLE_RATE)
        if len(speech) < min_len:
            raise ServiceError(
                f"Pas assez de parole ({len(speech) / SAMPLE_RATE:.1f} s, "
                f"minimum {self.settings['duree_min_enrolement']} s)"
            )
        size = int(_CHUNK_SECONDS * SAMPLE_RATE)
        pieces = [speech[i : i + size] for i in range(0, len(speech), size)]
        if len(pieces) > 1 and len(pieces[-1]) < min_len:
            last = pieces.pop()
            pieces[-1] = np.concatenate([pieces[-1], last])
        today = dt.datetime.now()
        for piece in pieces:
            emb = self.embedder.embed(piece)
            clip = None
            if self.settings["garder_audio"]:
                clip = f"clips/echantillons/{today:%Y/%m}/{uuid.uuid4().hex}.wav"
                write_wav(self.config.data_dir / clip, piece)
            self._add_sample(person_id, emb, clip, transcript, source, None,
                             len(piece) / SAMPLE_RATE, reload=False)
        self.reload()
        return len(pieces)

    # ------------------------------------------------------------------ personnes
    def _person(self, person_id: int) -> dict:
        row = self.db.one("SELECT * FROM personnes WHERE id=?", (person_id,))
        if row is None:
            raise ServiceError("Personne introuvable")
        return dict(row)

    def _require_model(self) -> None:
        if self.embedder is None or self.vad is None:
            raise ServiceError(f"Modèle non chargé : {self.model_error or 'chargement en cours'}")

    def create_person(self, nom: str, person_entity: str | None = None) -> int:
        nom = (nom or "").strip()
        if not nom:
            raise ServiceError("Nom obligatoire")
        try:
            cur = self.db.execute(
                "INSERT INTO personnes(nom, person_entity, cree_le) VALUES(?,?,?)",
                (nom, (person_entity or "").strip() or None, time.time()),
            )
        except sqlite3.IntegrityError as err:
            raise ServiceError(f"« {nom} » existe déjà") from err
        self.reload()
        return int(cur.lastrowid)

    def find_person(self, ref: int | str) -> int:
        """Retrouve une personne par id ou par nom (insensible à la casse)."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            row = self.db.one("SELECT id FROM personnes WHERE id=?", (int(ref),))
        else:
            row = self.db.one("SELECT id FROM personnes WHERE lower(nom)=lower(?)", (ref.strip(),))
        if row is None:
            raise ServiceError(f"Personne introuvable : {ref}")
        return int(row["id"])

    def update_person(self, person_id: int, nom: str | None, person_entity: str | None) -> None:
        self._person(person_id)
        if nom is not None:
            if not nom.strip():
                raise ServiceError("Nom obligatoire")
            try:
                self.db.execute("UPDATE personnes SET nom=? WHERE id=?", (nom.strip(), person_id))
            except sqlite3.IntegrityError as err:
                raise ServiceError(f"« {nom} » existe déjà") from err
        if person_entity is not None:
            self.db.execute("UPDATE personnes SET person_entity=? WHERE id=?",
                            (person_entity.strip() or None, person_id))
        self.reload()

    def delete_person(self, person_id: int) -> None:
        clips = [r["clip"] for r in self.db.all(
            "SELECT clip FROM echantillons WHERE personne_id=?", (person_id,))]
        with self.db.tx() as conn:
            conn.execute("DELETE FROM echantillons WHERE personne_id=?", (person_id,))
            conn.execute("UPDATE phrases SET personne_id=NULL, etat='nouvelle' WHERE personne_id=?",
                         (person_id,))
            conn.execute("DELETE FROM personnes WHERE id=?", (person_id,))
        for clip in clips:
            self._delete_clip_if_unused(clip)
        if self.enrolement and self.enrolement.personne_id == person_id:
            self.enrolement = None
        self.reload()

    def list_persons(self) -> list[dict]:
        model = self.embedder.key if self.embedder else self.model_key
        rows = self.db.all("SELECT * FROM personnes ORDER BY nom")
        counts: dict[int, dict[str, int]] = {}
        for r in self.db.all(
            "SELECT personne_id, source, COUNT(*) AS n FROM echantillons WHERE modele=? "
            "GROUP BY personne_id, source", (model,),
        ):
            counts.setdefault(r["personne_id"], {})[r["source"]] = r["n"]
        mapping = self.settings.get("personnes_ha") or {}
        out = []
        for r in rows:
            c = counts.get(r["id"], {})
            out.append({
                "id": r["id"], "nom": r["nom"],
                "person_entity": r["person_entity"] or mapping.get(r["nom"]),
                "echantillons": sum(c.values()), "par_source": c,
            })
        return out

    def list_samples(self, person_id: int) -> list[dict]:
        rows = self.db.all(
            "SELECT id, clip, transcript, source, duree, cree_le, modele, phrase_id FROM echantillons "
            "WHERE personne_id=? ORDER BY cree_le DESC", (person_id,),
        )
        return [dict(r) | {"audio": bool(r["clip"])} for r in rows]

    def delete_sample(self, sample_id: int) -> None:
        row = self.db.one("SELECT clip, phrase_id FROM echantillons WHERE id=?", (sample_id,))
        if row is None:
            raise ServiceError("Échantillon introuvable")
        self._delete_sample_row(sample_id, row["clip"])
        self.reload()

    # ------------------------------------------------------------------ journal
    def list_phrases(self, limit: int = 100, offset: int = 0, filtre: str | None = None) -> list[dict]:
        where, params = "", []
        if filtre == "inconnu":
            where = "WHERE p.resultat='inconnu' AND p.etat='nouvelle'"
        elif filtre in ("identifie", "trop_court", "enrolement", "reprise", "erreur"):
            where, params = "WHERE p.resultat=?", [filtre]
        elif filtre == "attribuee":
            where = "WHERE p.etat='attribuee'"
        rows = self.db.all(
            f"SELECT p.id, p.horodatage, p.satellite, p.transcript, p.resultat, p.personne_id, "
            f"p.score, p.marge, p.cluster_id, p.duree_parole, p.clip, p.etat, p.empreinte IS NOT NULL "
            f"AS a_empreinte, p.temps_empreinte_ms, p.temps_ajoute_ms, pe.nom, c.nom AS cluster_nom "
            f"FROM phrases p LEFT JOIN personnes pe ON pe.id=p.personne_id "
            f"LEFT JOIN clusters c ON c.id=p.cluster_id {where} "
            f"ORDER BY p.horodatage DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["audio"] = bool(d.pop("clip"))
            if d["cluster_id"] is not None and not d["cluster_nom"]:
                d["cluster_nom"] = f"Voix inconnue {d['cluster_id']}"
            out.append(d)
        return out

    def _phrase(self, phrase_id: int) -> sqlite3.Row:
        row = self.db.one("SELECT * FROM phrases WHERE id=?", (phrase_id,))
        if row is None:
            raise ServiceError("Phrase introuvable")
        return row

    def _phrase_embedding(self, row: sqlite3.Row) -> np.ndarray:
        """Empreinte d'une phrase dans le modèle courant (recalculée depuis l'audio si besoin)."""
        self._require_model()
        if row["empreinte"] is not None and row["modele"] == self.embedder.key:
            return from_blob(row["empreinte"])
        if not row["clip"] or not (self.config.data_dir / row["clip"]).exists():
            raise ServiceError("Pas d'empreinte ni d'audio pour cette phrase")
        speech = self.vad.speech(read_wav(self.config.data_dir / row["clip"]))
        if len(speech) < 0.5 * SAMPLE_RATE:
            raise ServiceError("Phrase trop courte pour servir d'échantillon")
        emb = self.embedder.embed(speech)
        self.db.execute("UPDATE phrases SET empreinte=?, modele=? WHERE id=?",
                        (to_blob(emb), self.embedder.key, row["id"]))
        return emb

    def _detach_from_cluster(self, phrase_ids: list[int]) -> None:
        clusters = {r["cluster_id"] for r in self.db.all(
            f"SELECT DISTINCT cluster_id FROM phrases WHERE id IN ({','.join('?' * len(phrase_ids))}) "
            "AND cluster_id IS NOT NULL", phrase_ids)}
        self.db.execute(
            f"UPDATE phrases SET cluster_id=NULL WHERE id IN ({','.join('?' * len(phrase_ids))})",
            phrase_ids,
        )
        for cid in clusters:
            self._refresh_cluster(cid)

    def _refresh_cluster(self, cluster_id: int) -> None:
        """Recalcule centroïde et taille d'un groupe ; le supprime s'il est vide."""
        rows = self.db.all(
            "SELECT empreinte, horodatage FROM phrases WHERE cluster_id=? AND empreinte IS NOT NULL",
            (cluster_id,),
        )
        if not rows:
            self.db.execute("UPDATE phrases SET cluster_id=NULL WHERE cluster_id=?", (cluster_id,))
            self.db.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
            return
        cent = clustering.centroid_of(np.stack([from_blob(r["empreinte"]) for r in rows]))
        self.db.execute(
            "UPDATE clusters SET centroide=?, taille=?, derniere_apparition=? WHERE id=?",
            (to_blob(cent), len(rows), max(r["horodatage"] for r in rows), cluster_id),
        )

    def assign_phrase(self, phrase_id: int, person_id: int, reload: bool = True) -> None:
        """Attribue (ou corrige) une phrase : elle devient un échantillon de la personne."""
        self._person(person_id)
        row = self._phrase(phrase_id)
        if row["resultat"] == "reprise":
            # Trop courte pour une empreinte fiable : on corrige l'étiquette, sans échantillon.
            self.db.execute("UPDATE phrases SET personne_id=?, etat='attribuee' WHERE id=?",
                            (person_id, phrase_id))
            return
        emb = self._phrase_embedding(row)
        # Retire l'échantillon éventuel (auto ou attribution) d'une autre personne.
        for s in self.db.all("SELECT id FROM echantillons WHERE phrase_id=?", (phrase_id,)):
            self.db.execute("DELETE FROM echantillons WHERE id=?", (s["id"],))
        self._detach_from_cluster([phrase_id])
        self.db.execute("UPDATE phrases SET personne_id=?, etat='attribuee' WHERE id=?",
                        (person_id, phrase_id))
        self._add_sample(person_id, emb, row["clip"], row["transcript"], "attribution",
                         phrase_id, row["duree_parole"], reload=False)
        if reload:
            self.reload()

    def unassign_phrase(self, phrase_id: int) -> None:
        """« Ce n'était personne de connu » : retire l'échantillon, remet la phrase en inconnue."""
        row = self._phrase(phrase_id)
        for s in self.db.all("SELECT id FROM echantillons WHERE phrase_id=?", (phrase_id,)):
            self.db.execute("DELETE FROM echantillons WHERE id=?", (s["id"],))
        # Une reprise retirée redevient ce qu'elle était : une phrase trop courte.
        resultat = "trop_court" if row["resultat"] == "reprise" else "inconnu"
        self.db.execute(
            "UPDATE phrases SET personne_id=NULL, etat='nouvelle', resultat=? WHERE id=?",
            (resultat, phrase_id),
        )
        self.reload()
        if row["empreinte"] is not None and self.embedder and row["modele"] == self.embedder.key:
            with self._lock:
                cid = self._assign_cluster(from_blob(row["empreinte"]))
            self.db.execute("UPDATE phrases SET cluster_id=? WHERE id=?", (cid, phrase_id))

    def ignore_phrase(self, phrase_id: int) -> None:
        row = self._phrase(phrase_id)
        if row["empreinte"] is not None and row["modele"]:
            self.db.execute(
                "INSERT INTO ignores(empreinte, modele, raison, phrase_id, cree_le) VALUES(?,?,?,?,?)",
                (row["empreinte"], row["modele"], "phrase", phrase_id, time.time()),
            )
        self._detach_from_cluster([phrase_id])
        self.db.execute("UPDATE phrases SET etat='ignoree' WHERE id=?", (phrase_id,))
        self.reload()

    def delete_phrase(self, phrase_id: int) -> None:
        row = self._phrase(phrase_id)
        self._detach_from_cluster([phrase_id])
        # Les échantillons tirés de la phrase restent : on garde l'apprentissage.
        self.db.execute("DELETE FROM phrases WHERE id=?", (phrase_id,))
        self._delete_clip_if_unused(row["clip"])
        self.reload()

    def phrase_clip(self, phrase_id: int) -> Path:
        return self._clip_file(self._phrase(phrase_id)["clip"])

    def sample_clip(self, sample_id: int) -> Path:
        row = self.db.one("SELECT clip FROM echantillons WHERE id=?", (sample_id,))
        if row is None:
            raise ServiceError("Échantillon introuvable")
        return self._clip_file(row["clip"])

    def _clip_file(self, clip: str | None) -> Path:
        if not clip:
            raise ServiceError("Pas d'audio")
        path = (self.config.data_dir / clip).resolve()
        if not path.is_relative_to(self.config.data_dir.resolve()) or not path.exists():
            raise ServiceError("Audio introuvable")
        return path

    # ------------------------------------------------------------------ groupes
    def list_clusters(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM clusters ORDER BY taille DESC, derniere_apparition DESC")
        out = []
        for r in rows:
            extraits = self.db.all(
                "SELECT id, transcript, horodatage, clip IS NOT NULL AS audio FROM phrases "
                "WHERE cluster_id=? ORDER BY (clip IS NULL), duree_parole DESC LIMIT 3", (r["id"],),
            )
            out.append({
                "id": r["id"], "nom": r["nom"] or f"Voix inconnue {r['id']}",
                "taille": r["taille"], "derniere_apparition": r["derniere_apparition"],
                "cree_le": r["cree_le"], "extraits": [dict(e) for e in extraits],
            })
        return out

    def _cluster_phrase_ids(self, cluster_id: int) -> list[int]:
        if self.db.one("SELECT id FROM clusters WHERE id=?", (cluster_id,)) is None:
            raise ServiceError("Groupe introuvable")
        return [r["id"] for r in self.db.all("SELECT id FROM phrases WHERE cluster_id=?", (cluster_id,))]

    def assign_cluster(self, cluster_id: int, person_id: int) -> int:
        ids = self._cluster_phrase_ids(cluster_id)
        done = 0
        for pid in ids:
            try:
                self.assign_phrase(pid, person_id, reload=False)
                done += 1
            except ServiceError as err:
                _LOGGER.warning("Phrase %d non attribuée : %s", pid, err)
        self.db.execute("UPDATE phrases SET cluster_id=NULL WHERE cluster_id=?", (cluster_id,))
        self.db.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
        self.reload()
        return done

    def rename_cluster(self, cluster_id: int, nom: str | None) -> None:
        self._cluster_phrase_ids(cluster_id)
        self.db.execute("UPDATE clusters SET nom=? WHERE id=?", ((nom or "").strip() or None, cluster_id))

    def merge_clusters(self, source_id: int, target_id: int) -> None:
        if source_id == target_id:
            raise ServiceError("Choisir deux groupes différents")
        self._cluster_phrase_ids(source_id)
        self._cluster_phrase_ids(target_id)
        self.db.execute("UPDATE phrases SET cluster_id=? WHERE cluster_id=?", (target_id, source_id))
        self.db.execute("DELETE FROM clusters WHERE id=?", (source_id,))
        self._refresh_cluster(target_id)
        self.reload()

    def ignore_cluster(self, cluster_id: int) -> None:
        ids = self._cluster_phrase_ids(cluster_id)
        row = self.db.one("SELECT centroide, modele FROM clusters WHERE id=?", (cluster_id,))
        now = time.time()
        with self.db.tx() as conn:
            if row["centroide"] is not None:
                conn.execute(
                    "INSERT INTO ignores(empreinte, modele, raison, cree_le) VALUES(?,?,?,?)",
                    (row["centroide"], row["modele"], f"groupe {cluster_id}", now),
                )
            # Quelques phrases du groupe en plus du centroïde, pour couvrir sa variabilité.
            for p in conn.execute(
                "SELECT id, empreinte, modele FROM phrases WHERE cluster_id=? AND empreinte IS NOT NULL "
                "ORDER BY horodatage DESC LIMIT 20", (cluster_id,),
            ).fetchall():
                conn.execute(
                    "INSERT INTO ignores(empreinte, modele, raison, phrase_id, cree_le) VALUES(?,?,?,?,?)",
                    (p["empreinte"], p["modele"], f"groupe {cluster_id}", p["id"], now),
                )
            conn.execute("UPDATE phrases SET etat='ignoree', cluster_id=NULL WHERE cluster_id=?",
                         (cluster_id,))
            conn.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
        _LOGGER.info("Groupe %d ignoré (%d phrases)", cluster_id, len(ids))
        self.reload()

    def delete_cluster(self, cluster_id: int) -> None:
        ids = self._cluster_phrase_ids(cluster_id)
        clips = [r["clip"] for r in self.db.all("SELECT clip FROM phrases WHERE cluster_id=?", (cluster_id,))]
        with self.db.tx() as conn:
            conn.execute("DELETE FROM phrases WHERE cluster_id=?", (cluster_id,))
            conn.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
        for clip in clips:
            self._delete_clip_if_unused(clip)
        _LOGGER.info("Groupe %d supprimé (%d phrases)", cluster_id, len(ids))
        self.reload()

    def list_ignores(self) -> list[dict]:
        return [dict(r) for r in self.db.all(
            "SELECT id, raison, phrase_id, modele, cree_le FROM ignores ORDER BY cree_le DESC")]

    def delete_ignore(self, ignore_id: int) -> None:
        self.db.execute("DELETE FROM ignores WHERE id=?", (ignore_id,))
        self.reload()

    def recluster(self) -> int:
        """Recalcul complet des groupes de voix inconnues."""
        self._require_model()
        model = self.embedder.key
        rows = self.db.all(
            "SELECT id, empreinte, horodatage FROM phrases WHERE resultat='inconnu' AND etat='nouvelle' "
            "AND empreinte IS NOT NULL AND modele=? ORDER BY horodatage", (model,),
        )
        with self._lock:
            ignores = self._ignores
        keep = []
        for r in rows:
            emb = from_blob(r["empreinte"])
            if ignores.size and float(np.max(ignores @ emb)) >= self.settings["seuil_ignore"]:
                self.db.execute("UPDATE phrases SET etat='ignoree', cluster_id=NULL WHERE id=?", (r["id"],))
                continue
            keep.append((r, emb))
        labels = (
            clustering.agglomerative(np.stack([e for _, e in keep]), self.settings["seuil_cluster"])
            if keep else np.zeros(0, dtype=int)
        )
        names = {r["id"]: r["nom"] for r in self.db.all("SELECT id, nom FROM clusters WHERE nom IS NOT NULL")}
        old_of = {r["id"]: r["cluster_id"] for r in self.db.all(
            "SELECT id, cluster_id FROM phrases WHERE cluster_id IS NOT NULL")}
        now = time.time()
        with self.db.tx() as conn:
            conn.execute("UPDATE phrases SET cluster_id=NULL WHERE cluster_id IS NOT NULL")
            conn.execute("DELETE FROM clusters")
            for label in range(int(labels.max()) + 1 if len(labels) else 0):
                members = [keep[i] for i in np.flatnonzero(labels == label)]
                embs = np.stack([e for _, e in members])
                # On reprend le nom donné à la main au groupe majoritaire d'origine.
                olds = [old_of.get(r["id"]) for r, _ in members if old_of.get(r["id"]) in names]
                nom = names[max(set(olds), key=olds.count)] if olds else None
                cur = conn.execute(
                    "INSERT INTO clusters(nom, centroide, taille, modele, cree_le, derniere_apparition) "
                    "VALUES(?,?,?,?,?,?)",
                    (nom, to_blob(clustering.centroid_of(embs)), len(members), model, now,
                     max(r["horodatage"] for r, _ in members)),
                )
                cid = cur.lastrowid
                conn.executemany("UPDATE phrases SET cluster_id=? WHERE id=?",
                                 [(cid, r["id"]) for r, _ in members])
        self.reload()
        return int(labels.max()) + 1 if len(labels) else 0

    # --------------------------------------------------------- tâches de fond
    def run_task(self, name: str, func: Callable[[], Any]) -> bool:
        """Lance une tâche longue dans un thread (une seule à la fois)."""
        if self.task.get("etat") == "en_cours":
            return False
        self.task = {"nom": name, "etat": "en_cours", "progression": 0.0, "message": "", "debut": time.time()}

        def runner() -> None:
            try:
                result = func()
                self.task.update(etat="termine", progression=1.0, resultat=result, fin=time.time())
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Tâche %s en échec", name)
                self.task.update(etat="erreur", message=str(err), fin=time.time())

        threading.Thread(target=runner, name=f"whospeaks-{name}", daemon=True).start()
        return True

    def change_model_async(self) -> None:
        def job() -> dict:
            self.load_models()
            if self.embedder is None or self.embedder.key != resolve_speaker_model(self.model_key).key:
                raise ServiceError(self.model_error or "Modèle non chargé")
            return self.reembed_all()

        self.run_task("changement_modele", job)

    def reembed_all(self) -> dict:
        """Recalcule toutes les empreintes avec le modèle courant, depuis l'audio conservé."""
        self._require_model()
        model = self.embedder.key
        samples = self.db.all("SELECT id, clip FROM echantillons WHERE modele<>?", (model,))
        phrases = self.db.all(
            "SELECT id, clip FROM phrases WHERE empreinte IS NOT NULL AND (modele IS NULL OR modele<>?)",
            (model,),
        )
        total = max(1, len(samples) + len(phrases))
        done = ok = lost = 0
        for kind, rows in (("echantillons", samples), ("phrases", phrases)):
            for r in rows:
                emb = None
                path = self.config.data_dir / r["clip"] if r["clip"] else None
                if path is not None and path.exists():
                    speech = self.vad.speech(read_wav(path))
                    if len(speech) >= 0.5 * SAMPLE_RATE:
                        emb = self.embedder.embed(speech)
                if emb is not None:
                    self.db.execute(f"UPDATE {kind} SET empreinte=?, modele=? WHERE id=?",
                                    (to_blob(emb), model, r["id"]))
                    ok += 1
                else:
                    lost += 1  # gardé tel quel, inutilisable avec ce modèle
                done += 1
                self.task["progression"] = done / total
        # Les listes à ignorer se recalculent depuis les phrases concernées.
        for ign in self.db.all("SELECT id, phrase_id FROM ignores WHERE modele<>?", (model,)):
            p = self.db.one("SELECT empreinte, modele FROM phrases WHERE id=?", (ign["phrase_id"],)) \
                if ign["phrase_id"] else None
            if p is not None and p["modele"] == model:
                self.db.execute("UPDATE ignores SET empreinte=?, modele=? WHERE id=?",
                                (p["empreinte"], model, ign["id"]))
        self.reload()
        groupes = self.recluster()
        return {"recalcules": ok, "sans_audio": lost, "groupes": groupes}

    def purge_clips(self) -> int:
        """Supprime l'audio plus ancien que la durée de conservation."""
        days = float(self.settings["retention_jours"])
        if days <= 0:
            return 0
        limit = time.time() - days * 86400
        keep_samples = bool(self.settings["garder_audio_echantillons"])
        removed = 0
        for r in self.db.all(
            "SELECT id, clip FROM phrases WHERE clip IS NOT NULL AND horodatage < ?", (limit,)
        ):
            in_sample = self.db.one("SELECT 1 FROM echantillons WHERE clip=?", (r["clip"],))
            if in_sample and keep_samples:
                continue
            self.db.execute("UPDATE phrases SET clip=NULL WHERE id=?", (r["id"],))
            if not keep_samples:
                self.db.execute("UPDATE echantillons SET clip=NULL WHERE clip=?", (r["clip"],))
            self._delete_clip_if_unused(r["clip"])
            removed += 1
        if not keep_samples:
            for r in self.db.all(
                "SELECT id, clip FROM echantillons WHERE clip IS NOT NULL AND cree_le < ?", (limit,)
            ):
                self.db.execute("UPDATE echantillons SET clip=NULL WHERE id=?", (r["id"],))
                self._delete_clip_if_unused(r["clip"])
                removed += 1
        if removed:
            _LOGGER.info("Rétention : %d extraits audio supprimés", removed)
        return removed

    # -------------------------------------------------------------- calibrage
    def person_embeddings(self) -> dict[int, np.ndarray]:
        with self._lock:
            return dict(self._persons)

    def calibrate(self, max_error: float = 0.02) -> dict:
        persons = self.person_embeddings()
        if len(persons) < 2:
            raise ServiceError("Il faut au moins deux personnes avec des échantillons")
        report = calibration.calibrate(persons, self.settings["agregation"], max_error)
        names = {pid: m["nom"] for pid, m in self._person_meta.items()}
        prop = report.get("proposition")
        if prop:
            trials = calibration.leave_one_out(persons, self.settings["agregation"])
            conf = calibration.confusion(trials, prop["seuil_identification"], prop["marge_min"])
            report["confusion"] = {
                names.get(t, str(t)): {
                    (names.get(int(k), k) if k != "inconnu" else k): v for k, v in row.items()
                }
                for t, row in conf.items()
            }
        actuel = calibration.apply_thresholds(
            calibration.leave_one_out(persons, self.settings["agregation"]),
            self.settings["seuil_identification"], self.settings["marge_min"],
        )
        report["actuel"] = {
            "seuil_identification": self.settings["seuil_identification"],
            "marge_min": self.settings["marge_min"],
            "precision": round(actuel.accuracy, 4),
            "taux_erreur": round(actuel.error_rate, 4),
        }
        return report

    # ------------------------------------------------------- export / import
    def export_zip(self, dest: Path, with_audio: bool = True) -> Path:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            with tempfile.TemporaryDirectory() as tmp:
                snap = Path(tmp) / "whospeaks.db"
                self.db.backup_to(snap)
                zf.write(snap, "whospeaks.db")
            if with_audio and self.config.clips_dir.exists():
                for path in self.config.clips_dir.rglob("*.wav"):
                    zf.write(path, path.relative_to(self.config.data_dir).as_posix(),
                             compress_type=zipfile.ZIP_STORED)
        return dest

    def import_zip(self, src: Path) -> dict:
        with zipfile.ZipFile(src) as zf:
            names = zf.namelist()
            if "whospeaks.db" not in names:
                raise ServiceError("Archive invalide : whospeaks.db absent")
            with tempfile.TemporaryDirectory() as tmp:
                zf.extract("whospeaks.db", tmp)
                candidate = Path(tmp) / "whospeaks.db"
                try:
                    check = sqlite3.connect(candidate)
                    ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    check.execute("SELECT COUNT(*) FROM personnes").fetchone()
                    check.close()
                except sqlite3.DatabaseError as err:
                    raise ServiceError(f"Base importée illisible : {err}") from err
                if not ok:
                    raise ServiceError("Base importée corrompue")
                # Sauvegarde de la base actuelle avant remplacement.
                stamp = time.strftime("%Y%m%d-%H%M%S")
                self.db.backup_to(self.config.data_dir / f"whospeaks.avant-import-{stamp}.db")
                with self.db._lock:  # noqa: SLF001
                    self.db.close()
                    for suffix in ("-wal", "-shm"):
                        side = self.config.db_path.with_name(self.config.db_path.name + suffix)
                        side.unlink(missing_ok=True)
                    shutil.copyfile(candidate, self.config.db_path)
                    self.db.reopen()
            clips = 0
            root = self.config.data_dir.resolve()
            for name in names:
                if not name.startswith("clips/") or name.endswith("/"):
                    continue
                target = (self.config.data_dir / name).resolve()
                if not target.is_relative_to(root):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src_f, open(target, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                clips += 1
        self.settings = self.db.load_settings(DEFAULT_SETTINGS)
        self.enrolement = None
        self.reload()
        return {"extraits": clips, "personnes": len(self._person_meta)}

    # ------------------------------------------------------------------ état
    def status(self) -> dict:
        counts = self.db.one(
            "SELECT (SELECT COUNT(*) FROM personnes) AS personnes, "
            "(SELECT COUNT(*) FROM echantillons) AS echantillons, "
            # Les reprises ne sont pas des identifications : comptées à part.
            "(SELECT COUNT(*) FROM phrases WHERE resultat<>'reprise') AS phrases, "
            "(SELECT COUNT(*) FROM phrases WHERE resultat='reprise') AS reprises, "
            "(SELECT COUNT(*) FROM clusters) AS groupes",
        )
        enrol = self._active_enrolement()
        return {
            "modele": self.model_key,
            "modele_charge": self.embedder.key if self.embedder else None,
            "modele_erreur": self.model_error,
            "vad": "silero" if self.vad and self.vad.is_silero else "energie",
            "enrolement": None if enrol is None else {
                "personne_id": enrol.personne_id, "personne": enrol.nom,
                "n": enrol.n, "total": enrol.total, "expire": enrol.expire,
            },
            "tache": {k: v for k, v in self.task.items() if k != "resultat"} | {
                "resultat": self.task.get("resultat")},
            **dict(counts),
        }
