import numpy as np

from whospeaks.engine import normalize
from whospeaks.rotation import most_redundant, to_remove


def test_most_redundant_picks_duplicate():
    a, b, c = normalize(np.array([1.0, 0, 0])), normalize(np.array([0, 1.0, 0])), normalize(np.array([0, 0, 1.0]))
    dup = normalize(np.array([1.0, 0.05, 0]))
    embs = np.stack([a, b, c, dup])
    assert most_redundant(embs) in (0, 3)


def test_to_remove_keeps_max_and_variety():
    rng = np.random.default_rng(0)
    base = normalize(rng.normal(size=16))
    near = [normalize(base + rng.normal(0, 0.01, 16)) for _ in range(6)]
    far = [normalize(rng.normal(size=16)) for _ in range(4)]
    embs = np.stack(near + far)
    removed = to_remove(embs, 5)
    assert len(removed) == 5
    # Les échantillons redondants partent en premier, les variés restent.
    assert all(i < 6 for i in removed)


def test_to_remove_prefers_removable():
    rng = np.random.default_rng(1)
    embs = np.stack([normalize(rng.normal(size=8)) for _ in range(6)])
    removable = np.array([False, False, False, True, True, True])
    removed = to_remove(embs, 4, removable)
    assert len(removed) == 2 and all(removable[i] for i in removed)
