import numpy as np

from whospeaks.calibration import (
    apply_thresholds, calibrate, confusion, eer, grid_search, leave_one_out, pair_scores,
)
from whospeaks.engine import normalize


def persons(k=3, n=12, dim=64, noise=0.25, seed=0):
    rng = np.random.default_rng(seed)
    out = {}
    for pid in range(1, k + 1):
        c = normalize(rng.normal(size=dim))
        out[pid] = np.stack([normalize(c + rng.normal(0, noise / np.sqrt(dim) * 4, dim)) for _ in range(n)])
    return out


def test_eer_perfect_and_overlap():
    rate, thr = eer(np.array([0.8, 0.9, 0.85]), np.array([0.1, 0.2, 0.3]))
    assert rate == 0.0 and 0.3 < thr <= 0.8
    rate, _ = eer(np.array([0.5, 0.6]), np.array([0.5, 0.6]))
    assert rate > 0.2


def test_pair_scores_counts():
    p = persons(k=2, n=4)
    g, i = pair_scores(p)
    assert len(g) == 2 * 6 and len(i) == 16


def test_leave_one_out_and_grid():
    p = persons()
    trials = leave_one_out(p)
    assert len(trials) == 36
    assert all(t.best_id == t.true_id for t in trials)
    seuil, marge, out = grid_search(trials, 0.02)
    assert out.error_rate <= 0.02 and out.accuracy > 0.9
    assert apply_thresholds(trials, 2.0, 0).unknown == 36  # seuil impossible : tout inconnu
    conf = confusion(trials, seuil, marge)
    assert conf[1].get("1", 0) >= 11


def test_calibrate_report():
    r = calibrate(persons())
    assert r["eer"] is not None and r["eer"] < 0.05
    assert r["proposition"]["taux_erreur"] <= 0.02
    assert len(r["hist_meme"]["counts"]) == 40
