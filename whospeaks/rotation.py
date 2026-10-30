"""Limitation du nombre d'échantillons par personne : on retire les plus redondants."""

from __future__ import annotations

import numpy as np


def most_redundant(embs: np.ndarray, removable: np.ndarray | None = None) -> int:
    """Indice de l'échantillon dont la similarité moyenne aux autres est la plus forte.

    `removable` (booléens) restreint le choix ; s'il ne contient aucun candidat,
    tous les échantillons sont éligibles.
    """
    n = len(embs)
    if n < 2:
        return 0
    sims = embs @ embs.T
    mean_sim = (sims.sum(axis=1) - np.diag(sims)) / (n - 1)
    if removable is not None and removable.any():
        mean_sim = np.where(removable, mean_sim, -np.inf)
    return int(np.argmax(mean_sim))


def to_remove(embs: np.ndarray, max_count: int, removable: np.ndarray | None = None) -> list[int]:
    """Indices à retirer pour revenir à `max_count` échantillons."""
    keep = list(range(len(embs)))
    removed: list[int] = []
    mask = None if removable is None else removable.copy()
    while len(keep) > max_count:
        sub = embs[keep]
        sub_mask = None if mask is None else mask[keep]
        local = most_redundant(sub, sub_mask)
        removed.append(keep.pop(local))
    return removed
