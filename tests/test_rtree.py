import math

import numpy as np
import pytest

from backend.indexes.rtree import RTree, SearchTrace, geo_weights, haversine_km, rtree_capacities
from backend.storage.page import PageFormatError
from backend.storage.rid import RID


def _rid(i: int) -> RID:
    return RID(i // 65 + 1, i % 65)


def _points(n: int, dims: int = 2, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).uniform(0, 100, (n, dims))


def _filled(tmp_path, counter, pts, page_size=1024, name="t.rtr", coord="d"):
    tree = RTree.create(tmp_path / name, pts.shape[1], page_size, counter, coord=coord)
    for i, p in enumerate(pts):
        tree.insert(p, None, _rid(i))
    return tree


def test_capacidades_desde_B():
    # entrada = 2·D·c + 6 (hoja) o + 4 (interno); 24 bytes de header
    assert rtree_capacities(4096, 2) == (107, 113)
    assert rtree_capacities(8192, 2) == ((8192 - 24) // 38, (8192 - 24) // 36)
    assert rtree_capacities(4096, 128, "f") == (3, 3)
    assert rtree_capacities(4096, 16, "f") == ((4096 - 24) // 134, (4096 - 24) // 132)


def test_no_cabe_en_la_pagina(tmp_path, counter):
    with pytest.raises(PageFormatError):
        RTree.create(tmp_path / "x.rtr", 128, 1024, counter)


def test_invariantes_tras_inserciones_aleatorias(tmp_path, counter):
    pts = _points(6000)
    tree = _filled(tmp_path, counter, pts)
    stats = tree.check_invariants()
    assert stats["entries"] == 6000 == tree.entry_count
    assert tree.height >= 3  # con páginas de 1 KB entran 25 entradas por hoja
    assert stats["leaves"] >= math.ceil(6000 / tree.leaf_capacity)


def test_busqueda_por_ventana_igual_a_fuerza_bruta(tmp_path, counter):
    pts = _points(5000)
    tree = _filled(tmp_path, counter, pts)
    rng = np.random.default_rng(3)
    for _ in range(25):
        a, b = rng.uniform(0, 100, 2), rng.uniform(0, 100, 2)
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        expected = {_rid(i) for i, p in enumerate(pts) if np.all(p >= lo) and np.all(p <= hi)}
        assert {rid for rid, _ in tree.search_window(lo, hi)} == expected


def test_la_ventana_poda_ramas(tmp_path, counter):
    pts = _points(5000)
    tree = _filled(tmp_path, counter, pts)
    counter.reset()
    list(tree.search_window([10, 10], [12, 12]))
    assert counter.disk_reads < tree.page_count / 10
    counter.reset()
    assert len(list(tree.search_window([0, 0], [100, 100]))) == 5000
    assert counter.disk_reads == tree.page_count - 1  # todo el árbol (sin la página 0)


def test_radio_igual_a_fuerza_bruta(tmp_path, counter):
    pts = _points(5000)
    tree = _filled(tmp_path, counter, pts)
    q = np.array([40.0, 60.0])
    dist = np.sqrt(((pts - q) ** 2).sum(axis=1))
    for r in (0.5, 3.0, 12.0):
        got = {rid: d for rid, _, d in tree.search_radius(q, r)}
        assert set(got) == {_rid(i) for i in np.flatnonzero(dist <= r)}
        assert all(d <= r for d in got.values())


def test_knn_exacto_y_en_orden(tmp_path, counter):
    pts = _points(5000)
    tree = _filled(tmp_path, counter, pts)
    rng = np.random.default_rng(5)
    for _ in range(10):
        q = rng.uniform(0, 100, 2)
        order = np.argsort(np.sqrt(((pts - q) ** 2).sum(axis=1)), kind="stable")
        for k in (1, 5, 20, 50):
            got = tree.knn(q, k)
            assert [rid for rid, _, _ in got] == [_rid(i) for i in order[:k]]
            assert [d for _, _, d in got] == sorted(d for _, _, d in got)


def test_knn_lee_pocas_paginas(tmp_path, counter):
    pts = _points(20_000)
    tree = RTree.create(tmp_path / "big.rtr", 2, 4096, counter)
    tree.bulk_load((p, _rid(i)) for i, p in enumerate(pts))
    counter.reset()
    trace = SearchTrace()
    tree.knn([50, 50], 10, trace=trace)
    assert counter.disk_reads == trace.visited <= 10  # de ~190 páginas
    assert trace.nodes and all(len(n["mbr"]) == 4 for n in trace.nodes)


def test_bulk_load_str_equivale_a_insertar(tmp_path, counter):
    pts = _points(8000)
    packed = RTree.create(tmp_path / "packed.rtr", 2, 1024, counter)
    counter.reset()
    packed.bulk_load((p, _rid(i)) for i, p in enumerate(pts))
    assert counter.disk_reads == 0 and counter.disk_writes == packed.page_count - 1  # 1 escritura por página
    stats = packed.check_invariants()
    assert stats["entries"] == 8000
    inserted = _filled(tmp_path, counter, pts, name="ins.rtr")
    assert packed.page_count < inserted.page_count  # hojas llenas
    q = [33.0, 71.0]
    assert [r for r, _, _ in packed.knn(q, 15)] == [r for r, _, _ in inserted.knn(q, 15)]
    lo, hi = [20, 20], [30, 45]
    assert {r for r, _ in packed.search_window(lo, hi)} == {r for r, _ in inserted.search_window(lo, hi)}


def test_bulk_load_solo_sobre_arbol_vacio(tmp_path, counter):
    tree = _filled(tmp_path, counter, _points(10))
    with pytest.raises(RuntimeError):
        tree.bulk_load(iter([([1.0, 2.0], RID(1, 0))]))


def test_borrado(tmp_path, counter):
    pts = _points(2000)
    tree = _filled(tmp_path, counter, pts)
    for i in range(0, 2000, 2):
        assert tree.delete(pts[i], None, _rid(i))
    assert not tree.delete(pts[0], None, _rid(0))
    assert tree.entry_count == 1000
    tree.check_invariants()
    assert {rid for rid, _ in tree.search_window([0, 0], [100, 100])} == {_rid(i) for i in range(1, 2000, 2)}


def test_persistencia(tmp_path, counter):
    pts = _points(3000)
    tree = _filled(tmp_path, counter, pts)
    expected = [rid for rid, _, _ in tree.knn([10, 90], 12)]
    height, pages = tree.height, tree.page_count
    tree.close()
    again = RTree.open(tmp_path / "t.rtr", 1024, counter)
    assert (again.height, again.page_count, again.entry_count, again.dims) == (height, pages, 3000, 2)
    assert [rid for rid, _, _ in again.knn([10, 90], 12)] == expected
    again.check_invariants()


def test_costo_de_insercion(tmp_path, counter):
    pts = _points(3000)
    tree = _filled(tmp_path, counter, pts)
    tree.flush()
    counter.reset()
    tree.insert([50.0, 50.0], None, RID(999, 0))
    # h lecturas hasta la hoja; al menos la escritura de la hoja
    assert counter.disk_reads == tree.height
    assert 1 <= counter.disk_writes <= 2 * tree.height + 1


@pytest.mark.parametrize("dims", [3, 8, 32])
def test_dimensiones_altas_con_float32(tmp_path, counter, dims):
    pts = np.random.default_rng(dims).uniform(0, 1, (800, dims)).astype(np.float32)
    tree = _filled(tmp_path, counter, pts, page_size=4096, name=f"d{dims}.rtr", coord="f")
    tree.check_invariants()
    q = pts[17].astype(np.float64)
    order = np.argsort(np.sqrt(((pts.astype(np.float64) - q) ** 2).sum(axis=1)), kind="stable")
    got = tree.knn(q, 5)
    assert got[0][2] == 0.0 and [r for r, _, _ in got] == [_rid(i) for i in order[:5]]


def test_distancia_geografica():
    # Plaza de Armas de Lima -> Parque Kennedy (Miraflores): ~8.4 km
    d = haversine_km(-77.0300, -12.0464, -77.0305, -12.1215)
    assert 8.2 < d < 8.5
    wx, wy = geo_weights(-12.1)
    assert 108 < wx < 109 and 111 < wy < 111.4
    # la distancia ponderada (equirectangular) no sobreestima la real
    approx = math.hypot((-77.0305 + 77.0300) * wx, (-12.1215 + 12.0464) * wy)
    assert approx <= d + 1e-6
