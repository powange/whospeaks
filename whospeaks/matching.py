"""Comparaison d'une empreinte avec les personnes enrôlées."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Match:
    person_id: int | None
    score: float  # meilleur score (personne retenue ou non)
    marge: float  # écart avec la deuxième personne
    best_id: int | None  # meilleure personne, même si non retenue
    second_id: int | None
    second_score: float


def person_score(sims: np.ndarray, agregation: str = "max") -> float:
    """Score d'une personne à partir des similarités avec ses échantillons."""
    if sims.size == 0:
        return -1.0
    if agregation == "top3":
        k = min(3, sims.size)
        return float(np.mean(np.partition(sims, -k)[-k:]))
    return float(np.max(sims))


def score_persons(
    emb: np.ndarray, persons: dict[int, np.ndarray], agregation: str = "max"
) -> dict[int, float]:
    """`persons` : id -> matrice (n_echantillons, dim) d'empreintes normalisées."""
    return {pid: person_score(mat @ emb, agregation) for pid, mat in persons.items() if len(mat)}


def decide(
    scores: dict[int, float], seuil: float, marge_min: float
) -> Match:
    if not scores:
        return Match(None, 0.0, 0.0, None, None, 0.0)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_id, best = ranked[0]
    second_id, second = ranked[1] if len(ranked) > 1 else (None, 0.0)
    # Une seule personne enrôlée : on compare à 0 (la marge ne peut pas départager).
    marge = best - max(second, 0.0)
    accepted = best >= seuil and marge >= marge_min
    return Match(
        person_id=best_id if accepted else None,
        score=best,
        marge=marge,
        best_id=best_id,
        second_id=second_id,
        second_score=second,
    )


def identify(
    emb: np.ndarray,
    persons: dict[int, np.ndarray],
    seuil: float,
    marge_min: float,
    agregation: str = "max",
) -> Match:
    return decide(score_persons(emb, persons, agregation), seuil, marge_min)
