import math

import numpy as np
import pytest

from backend.indexes.ivf import IVFError, IVFIndex, distances, kmeans
from backend.storage.rid import RID

D = 32


def _data(n: int = 6000, clusters: int = 30, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(clusters, D))
    return (centers[rng.integers(clusters, size=n)] + 0.5 * rng.normal(size=(n, D))).astype(np.float32)


def _rids(n: int) -> list[RID]:
    return [RID(i // 20 + 1, i % 20) for i in range(n)]


def _built(tmp_path, counter, x, metric="L2", lists=0, name="t_emb"):
    ix = IVFIndex.create(tmp_path / name, x.shape[1], 4096, counter, metric=metric, lists=lists)
    info = ix.build(lambda: zip(_rids(len(x)), x))
    return ix, info


def test_distancias():
    q = np.array([1.0, 0.0], dtype=np.float32)
    v = np.array([[1, 0], [0, 1], [-1, 0], [3, 4], [0, 0]], dtype=np.float32)
    assert distances("L2", q, v) == pytest.approx([0, math.sqrt(2), 2, math.sqrt(20), 1])
    assert distances("COSINE", q, v) == pytest.approx([0, 1, 2, 1 - 0.6, 1])  # vector nulo -> distancia 1


def test_kmeans_recupera_grupos_separados():
    rng = np.random.default_rng(1)
    centers = np.array([[0, 0], [10, 10], [-10, 10], [10, -10]], dtype=np.float32)
    x = (centers[rng.integers(4, size=2000)] + 0.3 * rng.normal(size=(2000, 2))).astype(np.float32)
    found = kmeans(x, 4)
    assert found.shape == (4, 2)
    for c in centers:
        assert np.min(np.linalg.norm(found - c, axis=1)) < 0.2
    assert len(kmeans(x[:3], 10)) == 3  # nunca más centroides que puntos
    with pytest.raises(IVFError):
        kmeans(np.zeros((0, 2), dtype=np.float32), 3)


@pytest.mark.parametrize("metric", ["L2", "COSINE"])
def test_construccion_reparte_todos_los_vectores(tmp_path, counter, metric):
    x = _data()
    ix, info = _built(tmp_path, counter, x, metric)
    c = int(math.sqrt(len(x)))
    assert info["lists"] == ix.num_lists == c  # C = floor(sqrt(N))
    assert ix.entry_count == len(x) == sum(ix.list_sizes())
    assert ix.list_capacity == (4096 - 24) // (6 + 4 * D)
    # con probes = C la búsqueda es exacta
    rng = np.random.default_rng(2)
    for q in x[rng.integers(len(x), size=10)]:
        truth = np.argsort(distances(metric, q, x), kind="stable")[:10]
        got = ix.search(q, 10, probes=c)
        assert got["scanned"] == len(x)
        assert {r for r, _ in got["hits"]} == {_rids(len(x))[i] for i in truth}
        assert [d for _, d in got["hits"]] == sorted(d for _, d in got["hits"])


def test_recall_crece_con_probes_y_el_io_tambien(tmp_path, counter):
    x = _data()
    ix, _ = _built(tmp_path, counter, x)
    rids = _rids(len(x))
    rng = np.random.default_rng(3)
    queries = x[rng.integers(len(x), size=40)] + 0.2 * rng.normal(size=(40, D)).astype(np.float32)
    recalls, reads = [], []
    for probes in (1, 4, 16, ix.num_lists):
        hit = total_reads = 0
        for q in queries:
            truth = {rids[i] for i in np.argsort(distances("L2", q, x), kind="stable")[:10]}
            counter.reset()
            got = ix.search(q, 10, probes)
            total_reads += counter.disk_reads
            hit += len(truth & {r for r, _ in got["hits"]})
        recalls.append(hit / (10 * len(queries)))
        reads.append(total_reads / len(queries))
    assert recalls == sorted(recalls) and recalls[-1] == 1.0 and recalls[1] > 0.8
    assert reads == sorted(reads)
    # solo se leen el directorio y las páginas de las celdas sondeadas
    assert reads[0] < 0.1 * reads[-1]
    assert reads[-1] == ix.dir.num_pages() - 1 + ix.lists.num_pages() - 1


def test_lists_configurable_y_validaciones(tmp_path, counter):
    x = _data(2000)
    ix, info = _built(tmp_path, counter, x, lists=12)
    assert info["lists"] == 12
    with pytest.raises(IVFError):
        IVFIndex.create(tmp_path / "bad", D, 4096, counter, metric="MANHATTAN")
    with pytest.raises(IVFError):
        ix.search(np.zeros(D + 1, dtype=np.float32), 5)


def test_insercion_y_borrado(tmp_path, counter):
    x = _data(3000)
    ix, _ = _built(tmp_path, counter, x)
    ix.flush()
    rng = np.random.default_rng(9)
    new = rng.normal(size=(50, D)).astype(np.float32)
    for i, v in enumerate(new):
        ix.insert(v, RID(9000, i))
    assert ix.entry_count == 3050 == sum(ix.list_sizes())
    for i, v in enumerate(new):
        hits = ix.search(v, 1, probes=2)["hits"]
        assert hits[0][0] == RID(9000, i) and hits[0][1] == pytest.approx(0.0, abs=1e-5)
    counter.reset()
    ix.insert(new[0] + 1, RID(9001, 0))
    # directorio completo + página de directorio de la celda + cabeza de su lista
    assert counter.disk_reads == ix.dir_pages + 2 and counter.disk_writes == 2
    for i, v in enumerate(new):
        assert ix.delete(v, RID(9000, i))
    assert not ix.delete(new[0], RID(9000, 0))
    assert ix.entry_count == 3001 == sum(ix.list_sizes())


def test_indice_vacio_y_entrenamiento_posterior(tmp_path, counter):
    ix = IVFIndex.create(tmp_path / "vacio", 8, 4096, counter)
    assert ix.search(np.zeros(8), 5)["hits"] == [] and not ix.trained and ix.num_lists == 1
    rng = np.random.default_rng(4)
    x = rng.normal(size=(200, 8)).astype(np.float32)
    for i, v in enumerate(x):
        ix.insert(v, RID(1, i))
    assert ix.needs_training  # ya hay datos para particionar
    # con una sola celda la búsqueda es un recorrido exacto
    truth = np.argsort(distances("L2", x[5], x), kind="stable")[:5]
    assert [r for r, _ in ix.search(x[5], 5)["hits"]] == [RID(1, int(i)) for i in truth]
    info = ix.build(lambda: ((RID(1, i), v) for i, v in enumerate(x)))
    assert info["lists"] == 14 and ix.trained and not ix.needs_training


def test_persistencia(tmp_path, counter):
    x = _data(2500)
    ix, _ = _built(tmp_path, counter, x, "COSINE")
    expected = ix.search(x[3], 8, 3)["hits"]
    lists = ix.num_lists
    ix.close()
    again = IVFIndex.open(tmp_path / "t_emb", 4096, counter)
    assert (again.num_lists, again.entry_count, again.metric, again.dims, again.trained) == (lists, 2500, "COSINE", D, True)
    assert again.search(x[3], 8, 3)["hits"] == expected
