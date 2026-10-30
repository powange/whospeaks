"""Base SQLite : schéma et accès (connexion unique protégée par un verrou)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS personnes (
    id INTEGER PRIMARY KEY,
    nom TEXT NOT NULL UNIQUE,
    person_entity TEXT,
    cree_le REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY,
    nom TEXT,
    centroide BLOB,
    taille INTEGER NOT NULL DEFAULT 0,
    modele TEXT,
    cree_le REAL NOT NULL,
    derniere_apparition REAL
);
CREATE TABLE IF NOT EXISTS phrases (
    id INTEGER PRIMARY KEY,
    horodatage REAL NOT NULL,
    satellite TEXT,
    transcript TEXT,
    -- identifie | inconnu | trop_court | enrolement | erreur
    resultat TEXT NOT NULL,
    personne_id INTEGER REFERENCES personnes(id) ON DELETE SET NULL,
    score REAL,
    marge REAL,
    cluster_id INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
    duree_parole REAL,
    clip TEXT,
    empreinte BLOB,
    modele TEXT,
    -- nouvelle | attribuee | ignoree
    etat TEXT NOT NULL DEFAULT 'nouvelle',
    temps_empreinte_ms REAL,
    temps_ajoute_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_phrases_horodatage ON phrases(horodatage);
CREATE INDEX IF NOT EXISTS idx_phrases_cluster ON phrases(cluster_id);
CREATE TABLE IF NOT EXISTS echantillons (
    id INTEGER PRIMARY KEY,
    personne_id INTEGER NOT NULL REFERENCES personnes(id) ON DELETE CASCADE,
    empreinte BLOB NOT NULL,
    modele TEXT NOT NULL,
    clip TEXT,
    transcript TEXT,
    -- enrolement | attribution | auto | fichier | navigateur
    source TEXT NOT NULL,
    phrase_id INTEGER REFERENCES phrases(id) ON DELETE SET NULL,
    duree REAL,
    cree_le REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_echantillons_personne ON echantillons(personne_id);
CREATE INDEX IF NOT EXISTS idx_echantillons_phrase ON echantillons(phrase_id);
CREATE TABLE IF NOT EXISTS ignores (
    id INTEGER PRIMARY KEY,
    empreinte BLOB NOT NULL,
    modele TEXT NOT NULL,
    raison TEXT,
    phrase_id INTEGER,
    cree_le REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reglages (
    cle TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);
"""


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def from_blob(blob: bytes | None) -> np.ndarray | None:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype="<f4").copy()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._connect()
        except sqlite3.DatabaseError as err:
            # Base corrompue : on la met de côté et on repart d'une base vide.
            backup = self.path.with_name(f"{self.path.name}.corrompue-{int(time.time())}")
            _LOGGER.error("Base illisible (%s), renommée en %s", err, backup.name)
            self.path.rename(backup)
            for suffix in ("-wal", "-shm"):
                side = self.path.with_name(self.path.name + suffix)
                if side.exists():
                    side.unlink()
            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            conn.close()
            raise sqlite3.DatabaseError(f"quick_check : {result}")
        conn.executescript(SCHEMA)
        return conn

    def reopen(self) -> None:
        with self._lock:
            self.conn.close()
            self.conn = self._open()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def all(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    # --- Réglages ---------------------------------------------------------
    def load_settings(self, defaults: dict) -> dict:
        settings = dict(defaults)
        for row in self.all("SELECT cle, valeur FROM reglages"):
            try:
                settings[row["cle"]] = json.loads(row["valeur"])
            except json.JSONDecodeError:
                _LOGGER.warning("Réglage illisible ignoré : %s", row["cle"])
        return settings

    def save_settings(self, values: dict) -> None:
        with self.tx() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO reglages(cle, valeur) VALUES(?, ?) "
                    "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                    (key, json.dumps(value)),
                )

    def backup_to(self, dest: Path) -> None:
        with self._lock:
            target = sqlite3.connect(dest)
            try:
                self.conn.backup(target)
            finally:
                target.close()
