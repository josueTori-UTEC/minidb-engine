import math
import random

import pytest

from backend.indexes.bplus_tree import BPlusTree, internal_capacity, leaf_capacity
from backend.storage.disk_manager import DiskCounter
from backend.storage.record import Column, ColumnType
from backend.storage.rid import RID

INT_COL = Column("id", ColumnType.INT)


def _tree(tmp_path, counter, page_size=1024, column=INT_COL, name="t.bpt"):
    return BPlusTree.create(tmp_path / name, column, page_size, counter)


def test_capacidades_desde_B():
    assert [leaf_capacity(b, 4) for b in (1024, 2048, 4096, 8192)] == [100, 202, 407, 816]
    assert [internal_capacity(b, 4) for b in (1024, 2048, 4096, 8192)] == [124, 252, 508, 1020]
    assert leaf_capacity(4096, 30) == (4096 - 24) // 36


def test_invariantes_tras_miles_de_inserts_aleatorios(tmp_path, counter):
    tree = _tree(tmp_path, counter)
    keys = list(range(20_000))
    random.Random(1).shuffle(keys)
    for k in keys:
        tree.insert(k, RID(k // 16 + 1, k % 16))
    stats = tree.check_invariants()
    assert stats["entries"] == 20_000
    # altura coherente con log_M N: entre el caso de nodos llenos y el de nodos al 50 %
    L, M = tree.leaf_capacity, tree.fan_out
    h_min = 1 + math.ceil(math.log(20_000 / L, M))
    h_max = 1 + math.ceil(math.log(20_000 / (L // 2), M // 2))
    assert h_min <= tree.height <= h_max
    assert tree.height == 3
    # split al 50 %: toda hoja salvo la raíz queda al menos a la mitad
    leaves = []
    leaf, _ = tree._descend(None)
    while leaf is not None:
        leaves.append(len(leaf.keys))
        leaf = tree._read_node(leaf.next_leaf) if leaf.next_leaf != -1 else None
    assert min(leaves) >= L // 2
    assert [k for k, _ in tree.iter_all()] == list(range(20_000))


def test_busqueda_puntual_cuesta_h_lecturas(tmp_path, counter):
    tree = _tree(tmp_path, counter)
    for k in random.Random(2).sample(range(100_000), 15_000):
        tree.insert(k, RID(1, 0))
    h = tree.height
    present = [k for k, _ in tree.iter_all()]
    for k in random.Random(3).sample(present, 300):
        counter.reset()
        assert tree.search(k) == [RID(1, 0)]
        reads, writes = counter.snapshot()
        assert writes == 0
        assert h <= reads <= h + 1  # +1 solo si k es separador de dos hojas
    counter.reset()
    for k in random.Random(4).sample(present, 1000):
        tree.search(k)
    assert counter.disk_reads / 1000 < h + 0.05


def test_rango(tmp_path, counter):
    tree = _tree(tmp_path, counter)
    keys = list(range(0, 30_000, 3))
    random.Random(5).shuffle(keys)
    for k in keys:
        tree.insert(k, RID(k + 1, 0))
    got = [k for k, _ in tree.range_search(300, 3000)]
    assert got == list(range(300, 3001, 3))
    assert [k for k, _ in tree.range_search(301, 305)] == [303]
    assert [k for k, _ in tree.range_search(300, 309, low_inclusive=False, high_inclusive=False)] == [303, 306]
    assert [k for k, _ in tree.range_search(None, 9)] == [0, 3, 6, 9]
    assert [k for k, _ in tree.range_search(29_990, None)] == [29_991, 29_994, 29_997]
    assert list(tree.range_search(40_000, 50_000)) == []
    # costo: h para bajar + hojas adicionales recorridas
    counter.reset()
    n = sum(1 for _ in tree.range_search(9_000, 12_000))
    leaves_touched = math.ceil(n / (tree.leaf_capacity // 2)) + 1
    assert counter.disk_reads <= tree.height + leaves_touched


def test_duplicados(tmp_path, counter):
    col = Column("dept", ColumnType.CHAR, 20)
    tree = _tree(tmp_path, counter, column=col)
    depts = [f"Dept {i:02d}" for i in range(8)]
    expected: dict[str, set] = {d: set() for d in depts}
    rng = random.Random(6)
    for i in range(6_000):
        d = rng.choice(depts)
        tree.insert(d, RID(i + 1, i % 7))
        expected[d].add(RID(i + 1, i % 7))
    tree.check_invariants()
    for d in depts:
        assert set(tree.search(d)) == expected[d]
    assert tree.search("Dept 99") == []
    assert [k for k, _ in tree.range_search("Dept 03", "Dept 04")].count("Dept 03") == len(expected["Dept 03"])


def test_muchos_duplicados_de_una_sola_clave(tmp_path, counter):
    tree = _tree(tmp_path, counter)
    for i in range(3_000):
        tree.insert(7, RID(i + 1, 0))
        tree.insert(i % 5, RID(i + 1, 1))
    tree.check_invariants()
    assert len(tree.search(7)) == 3_000
    assert len(tree.search(3)) == 600


def test_delete_sin_merge(tmp_path, counter):
    tree = _tree(tmp_path, counter)
    keys = list(range(5_000))
    random.Random(7).shuffle(keys)
    for k in keys:
        tree.insert(k, RID(k + 1, 0))
    for k in keys[:4_000]:
        counter.reset()
        assert tree.delete(k, RID(k + 1, 0))
        assert counter.disk_writes == 1
    assert not tree.delete(keys[0], RID(keys[0] + 1, 0))
    assert not tree.delete(keys[-1], RID(999_999, 0))  # clave existe pero RID no
    tree.check_invariants()
    assert sorted(k for k, _ in tree.iter_all()) == sorted(keys[4_000:])
    for k in keys[4_000:4_100]:
        assert tree.search(k) == [RID(k + 1, 0)]
    for k in keys[:100]:
        assert tree.search(k) == []
    # se puede seguir insertando tras borrar
    for k in keys[:500]:
        tree.insert(k, RID(1, 1))
    tree.check_invariants()


@pytest.mark.parametrize("page_size", [1024, 2048, 4096, 8192])
def test_persistencia_y_page_size(tmp_path, page_size):
    path = tmp_path / f"p{page_size}.bpt"
    tree = BPlusTree.create(path, INT_COL, page_size, DiskCounter())
    keys = random.Random(page_size).sample(range(10**6), 12_000)
    for k in keys:
        tree.insert(k, RID(k % 1000 + 1, k % 50))
    height, entries = tree.height, tree.entry_count
    tree.close()
    again = BPlusTree.open(path, INT_COL, page_size, DiskCounter())
    assert (again.height, again.entry_count) == (height, entries)
    again.check_invariants()
    for k in keys[:200]:
        assert again.search(k) == [RID(k % 1000 + 1, k % 50)]
    again.close()


def test_claves_float(tmp_path, counter):
    tree = _tree(tmp_path, counter, column=Column("salario", ColumnType.FLOAT))
    vals = [round(random.Random(8).uniform(0, 1), 6) + i for i in range(3_000)]
    for i, v in enumerate(vals):
        tree.insert(v, RID(i + 1, 0))
    tree.check_invariants()
    assert [k for k, _ in tree.range_search(10.0, 12.0)] == sorted(v for v in vals if 10.0 <= v <= 12.0)


def test_indice_unico_h_lecturas_exactas_y_rechaza_duplicados(tmp_path, counter):
    from backend.files.common import DuplicateKeyError

    tree = BPlusTree.create(tmp_path / "u.bpt", INT_COL, 1024, counter, unique=True)
    keys = list(range(30_000))
    random.Random(11).shuffle(keys)
    for k in keys:
        tree.insert(k, RID(k + 1, 0))
    tree.check_invariants()
    for k in keys[:2_000]:  # incluye claves que son separadores
        counter.reset()
        assert tree.search(k) == [RID(k + 1, 0)]
        assert counter.snapshot() == (tree.height, 0)
    with pytest.raises(DuplicateKeyError):
        tree.insert(keys[5], RID(1, 1))
    assert tree.entry_count == 30_000
    assert [k for k, _ in tree.range_search(100, 105, low_inclusive=False)] == [101, 102, 103, 104, 105]
    assert tree.delete(keys[5], RID(keys[5] + 1, 0)) and tree.search(keys[5]) == []
    tree.close()
    again = BPlusTree.open(tmp_path / "u.bpt", INT_COL, 1024, DiskCounter())
    assert again.unique


@pytest.mark.parametrize("n", [1, 2, 99, 100, 101, 5_000, 30_001])
def test_bulk_load_desde_entrada_ordenada(tmp_path, counter, n):
    tree = BPlusTree.create(tmp_path / f"bl{n}.bpt", INT_COL, 1024, counter, unique=True)
    tree.bulk_load((k * 2, RID(k + 1, 0)) for k in range(n))
    stats = tree.check_invariants()
    assert stats["entries"] == n
    # hojas llenas: ceil(n / L) hojas
    assert stats["leaves"] == -(-n // tree.leaf_capacity)
    for k in random.Random(n).sample(range(n), min(n, 200)):
        assert tree.search(k * 2) == [RID(k + 1, 0)]
        assert tree.search(k * 2 + 1) == []
    assert [k for k, _ in tree.range_search(10, 20)] == [k for k in range(10, 21, 2) if k < 2 * n]
    tree.insert(-1, RID(9, 9))  # se puede seguir insertando
    tree.insert(2 * n + 1, RID(9, 9))
    tree.check_invariants()
    assert (tree.min_key, tree.max_key) == (-1, 2 * n + 1)


def test_bulk_load_rechaza_desorden(tmp_path, counter):
    from backend.storage.page import PageFormatError

    tree = _tree(tmp_path, counter)
    with pytest.raises(PageFormatError):
        tree.bulk_load(iter([(2, RID(1, 0)), (1, RID(1, 1))]))
