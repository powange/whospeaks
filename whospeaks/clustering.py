"""Regroupement des voix inconnues.

- en ligne : algorithme « leader » (la phrase rejoint le groupe dont le centroïde est le
  plus proche, ou en crée un nouveau) ;
- recalcul complet : passe « leader » fine puis agglomération à liaison moyenne.
"""

from __future__ import annotations

import numpy as np

from .engine import normalize


def nearest(emb: np.ndarray, centroids: np.ndarray) -> tuple[int, float]:
    """Indice et similarité du centroïde le plus proche (-1 si aucun)."""
    if len(centroids) == 0:
        return -1, -1.0
    sims = centroids @ emb
    idx = int(np.argmax(sims))
    return idx, float(sims[idx])


def update_centroid(centroid: np.ndarray, size: int, emb: np.ndarray) -> np.ndarray:
    """Moyenne glissante (non normalisée stockée normalisée)."""
    return normalize(centroid * size + emb)


def centroid_of(embs: np.ndarray) -> np.ndarray:
    return normalize(embs.mean(axis=0))


def leader(embs: np.ndarray, seuil: float) -> np.ndarray:
    """Regroupement « leader » séquentiel ; renvoie l'étiquette de chaque ligne."""
    labels = np.full(len(embs), -1, dtype=int)
    sums: list[np.ndarray] = []
    cents: list[np.ndarray] = []
    for i, emb in enumerate(embs):
        if cents:
            idx, sim = nearest(emb, np.stack(cents))
            if sim >= seuil:
                labels[i] = idx
                sums[idx] = sums[idx] + emb
                cents[idx] = normalize(sums[idx])
                continue
        labels[i] = len(cents)
        sums.append(emb.copy())
        cents.append(emb.copy())
    return labels


def agglomerative(embs: np.ndarray, seuil: float, pre_seuil: float | None = None) -> np.ndarray:
    """Agglomération à liaison moyenne, arrêtée quand la similarité moyenne < seuil.

    Pour rester rapide sur quelques milliers de phrases, on part de micro-groupes
    formés par une passe « leader » à seuil plus strict (`pre_seuil`).
    """
    n = len(embs)
    if n == 0:
        return np.zeros(0, dtype=int)
    if pre_seuil is None:
        pre_seuil = min(0.95, seuil + 0.15)
    micro = leader(embs, pre_seuil) if n > 300 else np.arange(n)
    groups: list[list[int]] = [list(np.flatnonzero(micro == g)) for g in range(micro.max() + 1)]
    groups = [g for g in groups if g]

    k = len(groups)
    # Somme des similarités entre deux groupes = produit scalaire des sommes d'empreintes.
    totals = np.stack([embs[g].sum(axis=0) for g in groups]).astype(np.float64)
    sums = totals @ totals.T
    sizes = np.array([len(g) for g in groups], dtype=float)
    active = np.ones(k, dtype=bool)

    while active.sum() > 1:
        avg = sums / np.outer(sizes, sizes)
        np.fill_diagonal(avg, -np.inf)
        avg[~active, :] = -np.inf
        avg[:, ~active] = -np.inf
        i, j = np.unravel_index(int(np.argmax(avg)), avg.shape)
        if avg[i, j] < seuil:
            break
        # Fusion de j dans i (formule de Lance-Williams pour la liaison moyenne).
        sums[i, :] += sums[j, :]
        sums[:, i] += sums[:, j]
        sizes[i] += sizes[j]
        groups[i].extend(groups[j])
        groups[j] = []
        active[j] = False

    labels = np.full(n, -1, dtype=int)
    for new_label, gi in enumerate(np.flatnonzero(active)):
        for member in groups[gi]:
            labels[member] = new_label
    return labels
