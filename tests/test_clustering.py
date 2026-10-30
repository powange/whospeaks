import numpy as np

from whospeaks.clustering import agglomerative, leader, nearest, update_centroid
from whospeaks.engine import normalize


def blobs(n_per=10, k=3, dim=48, spread=0.15, seed=0):
    rng = np.random.default_rng(seed)
    centers = [normalize(rng.normal(size=dim)) for _ in range(k)]
    embs, truth = [], []
    for ci, c in enumerate(centers):
        for _ in range(n_per):
            embs.append(normalize(c + rng.normal(0, spread / np.sqrt(dim) * 4, dim)))
            truth.append(ci)
    order = rng.permutation(len(embs))
    return np.stack(embs)[order], np.array(truth)[order]


def same_partition(labels, truth):
    pairs = {(a, b) for a, b in zip(labels, truth)}
    return len(pairs) == len(set(labels)) == len(set(truth))


def test_nearest_and_centroid():
    c = np.stack([normalize(np.array([1.0, 0, 0])), normalize(np.array([0, 1.0, 0]))])
    idx, sim = nearest(normalize(np.array([0.9, 0.1, 0])), c)
    assert idx == 0 and sim > 0.9
    assert nearest(np.ones(3), np.zeros((0, 3)))[0] == -1
    cent = update_centroid(c[0], 1, c[1])
    assert abs(np.linalg.norm(cent) - 1) < 1e-6 and abs(cent[0] - cent[1]) < 1e-6


def test_leader_groups_ten_phrases_of_one_voice():
    embs, truth = blobs(n_per=10, k=1)
    assert len(set(leader(embs, 0.6))) == 1


def test_leader_separates_voices():
    embs, truth = blobs(n_per=10, k=3)
    assert same_partition(leader(embs, 0.6), truth)


def test_agglomerative_separates_voices():
    embs, truth = blobs(n_per=15, k=4, seed=3)
    assert same_partition(agglomerative(embs, 0.6), truth)


def test_agglomerative_large_uses_micro_clusters():
    embs, truth = blobs(n_per=120, k=3, seed=5)
    labels = agglomerative(embs, 0.6)
    assert same_partition(labels, truth)


def test_agglomerative_empty():
    assert len(agglomerative(np.zeros((0, 8)), 0.6)) == 0
