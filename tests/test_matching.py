import numpy as np

from whospeaks.engine import normalize
from whospeaks.matching import decide, identify, person_score


def test_person_score_max_and_top3():
    sims = np.array([0.2, 0.9, 0.5, 0.7])
    assert person_score(sims, "max") == 0.9
    assert abs(person_score(sims, "top3") - (0.9 + 0.7 + 0.5) / 3) < 1e-9
    assert abs(person_score(np.array([0.4, 0.6]), "top3") - 0.5) < 1e-9


def test_threshold_and_margin():
    m = decide({1: 0.70, 2: 0.50}, seuil=0.55, marge_min=0.08)
    assert m.person_id == 1 and abs(m.marge - 0.20) < 1e-9
    # Score sous le seuil
    assert decide({1: 0.50, 2: 0.10}, 0.55, 0.08).person_id is None
    # Marge insuffisante
    m = decide({1: 0.70, 2: 0.65}, 0.55, 0.08)
    assert m.person_id is None and m.best_id == 1 and m.second_id == 2


def test_single_person_and_empty():
    assert decide({3: 0.8}, 0.55, 0.08).person_id == 3
    m = decide({}, 0.55, 0.08)
    assert m.person_id is None and m.best_id is None


def test_identify_with_embeddings():
    rng = np.random.default_rng(1)
    a, b = normalize(rng.normal(size=32)), normalize(rng.normal(size=32))
    persons = {
        1: np.stack([normalize(a + rng.normal(0, 0.1, 32)) for _ in range(5)]),
        2: np.stack([normalize(b + rng.normal(0, 0.1, 32)) for _ in range(5)]),
    }
    probe = normalize(a + rng.normal(0, 0.1, 32))
    assert identify(probe, persons, 0.55, 0.08).person_id == 1
    assert identify(probe, persons, 0.55, 0.08, "top3").person_id == 1
