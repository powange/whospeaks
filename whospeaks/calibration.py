"""Calibrage des seuils et métriques d'évaluation (EER, précision, confusion)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .matching import decide, person_score


def pair_scores(persons: dict[int, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Similarités entre phrases d'une même personne et entre personnes différentes."""
    genuine: list[np.ndarray] = []
    impostor: list[np.ndarray] = []
    ids = list(persons)
    for a_i, a in enumerate(ids):
        ea = persons[a]
        if len(ea) > 1:
            sims = ea @ ea.T
            genuine.append(sims[np.triu_indices(len(ea), k=1)])
        for b in ids[a_i + 1 :]:
            impostor.append((ea @ persons[b].T).ravel())
    cat = lambda xs: np.concatenate(xs) if xs else np.zeros(0)  # noqa: E731
    return cat(genuine), cat(impostor)


def eer(genuine: np.ndarray, impostor: np.ndarray) -> tuple[float, float]:
    """Taux d'égale erreur et seuil correspondant."""
    if len(genuine) == 0 or len(impostor) == 0:
        return float("nan"), float("nan")
    thresholds = np.unique(np.concatenate([genuine, impostor]))
    g = np.sort(genuine)
    i = np.sort(impostor)
    frr = np.searchsorted(g, thresholds, side="left") / len(g)  # genuine < t
    far = 1.0 - np.searchsorted(i, thresholds, side="left") / len(i)  # impostor >= t
    k = int(np.argmin(np.abs(frr - far)))
    return float((frr[k] + far[k]) / 2), float(thresholds[k])


@dataclass
class Trial:
    true_id: int
    best_id: int | None
    score: float
    second_score: float

    @property
    def marge(self) -> float:
        return self.score - max(self.second_score, 0.0)


def leave_one_out(persons: dict[int, np.ndarray], agregation: str = "max") -> list[Trial]:
    """Chaque échantillon est identifié contre les autres (lui-même exclu)."""
    trials: list[Trial] = []
    for pid, mat in persons.items():
        for k in range(len(mat)):
            emb = mat[k]
            scores: dict[int, float] = {}
            for qid, qmat in persons.items():
                sims = qmat @ emb
                if qid == pid:
                    sims = np.delete(sims, k)
                if sims.size:
                    scores[qid] = person_score(sims, agregation)
            if pid not in scores:
                continue  # personne avec un seul échantillon : impossible à évaluer
            m = decide(scores, -np.inf, -np.inf)
            trials.append(Trial(pid, m.best_id, m.score, m.second_score))
    return trials


@dataclass
class Outcome:
    correct: int
    wrong: int
    unknown: int

    @property
    def total(self) -> int:
        return self.correct + self.wrong + self.unknown

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def error_rate(self) -> float:
        return self.wrong / self.total if self.total else 0.0


def apply_thresholds(trials: list[Trial], seuil: float, marge_min: float) -> Outcome:
    correct = wrong = unknown = 0
    for t in trials:
        if t.score >= seuil and t.marge >= marge_min:
            if t.best_id == t.true_id:
                correct += 1
            else:
                wrong += 1
        else:
            unknown += 1
    return Outcome(correct, wrong, unknown)


def grid_search(
    trials: list[Trial], max_error: float = 0.02, plancher: float | None = None
) -> tuple[float, float, Outcome] | None:
    """Seuils maximisant les bonnes identifications avec un taux d'erreur ≤ max_error.

    Plusieurs couples font souvent aussi bien : on prend le milieu de cette zone
    plutôt qu'un bord, pour garder de la marge sur des phrases nouvelles.
    `plancher` (en pratique le 99e centile des similarités entre personnes
    différentes) remonte le bas de la zone : une voix non enrôlée doit rester
    sous le seuil, ce que la validation croisée ne mesure pas.
    """
    if not trials:
        return None
    results: list[tuple[float, float, Outcome]] = []
    for seuil in np.round(np.arange(0.30, 0.91, 0.01), 2):
        for marge in np.round(np.arange(0.0, 0.31, 0.01), 2):
            out = apply_thresholds(trials, float(seuil), float(marge))
            if out.error_rate <= max_error:
                results.append((float(seuil), float(marge), out))
    if not results:
        return None
    top = max(r[2].correct for r in results)
    plateau = [r for r in results if r[2].correct == top]
    if plancher is not None and any(r[0] >= plancher for r in plateau):
        plateau = [r for r in plateau if r[0] >= plancher]
    seuils = sorted({r[0] for r in plateau})
    seuil = seuils[len(seuils) // 2]
    marges = sorted(r[1] for r in plateau if r[0] == seuil)
    marge = marges[len(marges) // 2]
    return next(r for r in plateau if r[0] == seuil and r[1] == marge)


def confusion(trials: list[Trial], seuil: float, marge_min: float) -> dict[int, dict[str, int]]:
    """true_id -> {str(prédit) ou 'inconnu' : nombre}."""
    out: dict[int, dict[str, int]] = {}
    for t in trials:
        accepted = t.score >= seuil and t.marge >= marge_min
        key = str(t.best_id) if accepted else "inconnu"
        row = out.setdefault(t.true_id, {})
        row[key] = row.get(key, 0) + 1
    return out


def histogram(values: np.ndarray, bins: int = 40) -> dict:
    counts, edges = np.histogram(values, bins=bins, range=(-0.2, 1.0))
    return {"counts": counts.tolist(), "edges": [round(float(e), 3) for e in edges]}


def calibrate(
    persons: dict[int, np.ndarray], agregation: str = "max", max_error: float = 0.02
) -> dict:
    """Rapport complet utilisé par la page Réglages et par le script `evaluate`."""
    genuine, impostor = pair_scores(persons)
    rate, thr = eer(genuine, impostor)
    trials = leave_one_out(persons, agregation)
    result: dict = {
        "personnes": len(persons),
        "echantillons": int(sum(len(m) for m in persons.values())),
        "paires_meme": int(len(genuine)),
        "paires_diff": int(len(impostor)),
        "eer": None if np.isnan(rate) else round(rate, 4),
        "seuil_eer": None if np.isnan(thr) else round(thr, 3),
        "hist_meme": histogram(genuine),
        "hist_diff": histogram(impostor),
        "essais": len(trials),
        "proposition": None,
    }
    plancher = float(np.quantile(impostor, 0.99)) if len(impostor) else None
    found = grid_search(trials, max_error, plancher)
    if found:
        seuil, marge, out = found
        result["proposition"] = {
            "seuil_identification": seuil,
            "marge_min": marge,
            "precision": round(out.accuracy, 4),
            "taux_erreur": round(out.error_rate, 4),
            "taux_inconnu": round(out.unknown / out.total, 4) if out.total else 0.0,
            "max_erreur": max_error,
        }
    return result
