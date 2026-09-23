"""Pruebas aleatorias diferenciales: cada estructura contra un modelo en memoria.

Se ejecutan secuencias largas de inserciones, borrados y búsquedas (con páginas
pequeñas para forzar splits, overflow y reorganizaciones) y, cada tanto, se
cierra y reabre el archivo. El resultado de la estructura en disco debe coincidir
siempre con el del modelo.
"""

import random

import pytest

from backend.database import Database
from backend.files.heap_file import HeapFile
from backend.files.sequential_file import SequentialFile
from backend.indexes.bplus_tree import BPlusTree
from backend.indexes.extendible_hash import ExtendibleHash
from backend.storage.disk_manager import DiskCounter
from backend.storage.record import Column, ColumnType
from backend.storage.rid import RID
from tests.conftest import make_row

INT_COL = Column("k", ColumnType.INT)


@pytest.mark.parametrize("seed", [1, 2])
def test_heap_contra_modelo(tmp_path, empleados_schema, seed):
    rng = random.Random(seed)
    path = tmp_path / "h.heap"
    heap = HeapFile.create(path, empleados_schema, 512, DiskCounter())
    model: dict[RID, tuple] = {}
    next_id = 0
    for step in range(4000):
        if model and rng.random() < 0.35:
            rid = rng.choice(list(model))
            assert heap.delete(rid) == model.pop(rid)
        else:
            row = make_row(next_id)
            next_id += 1
            rid = heap.insert(row)
            assert rid not in model
            model[rid] = row
        if step % 997 == 0:
            heap.close()
            heap = HeapFile.open(path, empleados_schema, 512, DiskCounter())
    assert dict(heap.scan()) == model
    assert heap.record_count == len(model)
    # todo slot libre es alcanzable desde la free-list
    for pid in heap.free_list():
        assert heap._load(pid).has_space()
    heap.close()


@pytest.mark.parametrize("seed", [3, 4, 5])
def test_sequential_contra_modelo(tmp_path, empleados_schema, seed):
    rng = random.Random(seed)
    main, ovf = tmp_path / "s.seq", tmp_path / "s.ovf"
    seq = SequentialFile.create(main, ovf, empleados_schema, 512, DiskCounter(), reorg_ratio=0.3)
    model: dict[int, tuple] = {}
    for step in range(3000):
        op = rng.random()
        if model and op < 0.25:
            key = rng.choice(list(model))
            (rid, values), = seq.find(key)
            assert values == model.pop(key)
            seq.delete(rid)
        elif op < 0.3 and model:
            lo = rng.randint(0, 5000)
            hi = lo + rng.randint(0, 400)
            got = [v[0] for _, v in seq.range_scan(lo, hi)]
            assert got == sorted(k for k in model if lo <= k <= hi)
        elif op < 0.32:
            seq.reorganize()
        else:
            key = rng.randint(0, 5000)
            if key in model:
                with pytest.raises(Exception):
                    seq.insert(make_row(key))
            else:
                seq.insert(make_row(key))
                model[key] = make_row(key)
                if seq.needs_reorganization():
                    seq.reorganize()
        if step % 701 == 0:
            seq.close()
            seq = SequentialFile.open(main, ovf, empleados_schema, 512, DiskCounter(), reorg_ratio=0.3)
    assert [v[0] for _, v in seq.scan()] == sorted(model)
    for key in rng.sample(range(5001), 300):
        assert [v for _, v in seq.find(key)] == ([model[key]] if key in model else [])
    assert seq.record_count == len(model)
    seq.close()


@pytest.mark.parametrize("unique", [False, True])
def test_bplus_contra_modelo(tmp_path, unique):
    rng = random.Random(10 + unique)
    path = tmp_path / "t.bpt"
    tree = BPlusTree.create(path, INT_COL, 512, DiskCounter(), unique=unique)
    model: list[tuple[int, RID]] = []
    serial = 0
    for step in range(6000):
        if model and rng.random() < 0.3:
            key, rid = model.pop(rng.randrange(len(model)))
            assert tree.delete(key, rid)
        else:
            key = rng.randint(0, 3000 if unique else 200)
            if unique and any(k == key for k, _ in model):
                continue
            serial += 1
            rid = RID(serial, serial % 7)
            tree.insert(key, rid)
            model.append((key, rid))
        if step % 1500 == 0:
            tree.check_invariants()
            tree.close()
            tree = BPlusTree.open(path, INT_COL, 512, DiskCounter())
    tree.check_invariants()
    for key in range(0, 3001 if unique else 201, 7):
        assert sorted(tree.search(key)) == sorted(r for k, r in model if k == key)
    lo, hi = 50, 120
    got = sorted((k, r) for k, r in tree.range_search(lo, hi))
    assert got == sorted((k, r) for k, r in model if lo <= k <= hi)
    tree.close()


def test_hash_contra_modelo(tmp_path):
    rng = random.Random(20)
    path = tmp_path / "t.hsh"
    h = ExtendibleHash.create(path, INT_COL, 512, DiskCounter(), max_depth=12)
    model: list[tuple[int, RID]] = []
    serial = 0
    for step in range(6000):
        if model and rng.random() < 0.3:
            key, rid = model.pop(rng.randrange(len(model)))
            assert h.delete(key, rid)
        else:
            key = rng.randint(0, 1500)
            serial += 1
            rid = RID(serial, 0)
            h.insert(key, rid)
            model.append((key, rid))
        if step % 1500 == 0:
            h.check_invariants()
            h.close()
            h = ExtendibleHash.open(path, INT_COL, 512, DiskCounter())
    h.check_invariants()
    for key in range(0, 1501, 3):
        assert sorted(h.search(key)) == sorted(r for k, r in model if k == key)
    h.close()


def test_sql_contra_modelo(tmp_path):
    """Tabla SEQUENTIAL con índices B+ y hash: SELECT por todos los caminos = modelo."""
    rng = random.Random(30)
    db = Database(tmp_path, page_size=1024)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, grupo INT, v FLOAT, s CHAR(8)) USING SEQUENTIAL")
    db.execute("CREATE INDEX ig ON t (grupo) USING BTREE")
    db.execute("CREATE INDEX ih ON t (id) USING HASH")
    model: dict[int, tuple] = {}
    for step in range(700):
        op = rng.random()
        if op < 0.55:
            rows = {}
            for _ in range(rng.randint(1, 8)):
                k = rng.randint(1, 3000)
                if k not in model and k not in rows:
                    rows[k] = (k, k % 13, round(k / 7, 3), f"s{k % 50}")
            if rows:
                db.execute("INSERT INTO t VALUES " + ", ".join(
                    f"({a}, {b}, {c}, '{d}')" for a, b, c, d in rows.values()))
                model.update(rows)
        elif op < 0.7 and model:
            g, cut = rng.randint(0, 12), rng.randint(1, 3000)
            res = db.execute(f"DELETE FROM t WHERE grupo = {g} AND id < {cut}")
            victims = [k for k, v in model.items() if v[1] == g and k < cut]
            assert res["message"] == f"{len(victims)} fila(s) eliminada(s)"
            for k in victims:
                del model[k]
        elif op < 0.8 and model:
            k = rng.choice(list(model))
            db.execute(f"DELETE FROM t WHERE id = {k}")
            del model[k]
        if step % 50 == 0 and model:
            k = rng.choice(list(model))
            assert db.execute(f"SELECT * FROM t WHERE id = {k}")["rows"] == [list(model[k])]
            g = rng.randint(0, 12)
            got = db.execute(f"SELECT id FROM t WHERE grupo = {g}", page_size=1000)["rows"]
            assert sorted(r[0] for r in got) == sorted(k for k, v in model.items() if v[1] == g)
            lo = rng.randint(1, 2500)
            got = db.execute(f"SELECT id FROM t WHERE id BETWEEN {lo} AND {lo + 300}", page_size=1000)
            assert [r[0] for r in got["rows"]] == sorted(k for k in model if lo <= k <= lo + 300)
            got = db.execute(f"SELECT id FROM t WHERE grupo >= 3 AND grupo <= 5 AND v > 100", page_size=5000)
            assert sorted(r[0] for r in got["rows"]) == sorted(
                k for k, v in model.items() if 3 <= v[1] <= 5 and v[2] > 100
            )
        if step % 233 == 0:
            db.close()
            db = Database(tmp_path, page_size=1024)
    assert _ids(db) == sorted(model)  # el Sequential devuelve en orden de PK
    t = db.tables["t"]
    t.indexes["ig"].structure.check_invariants()
    t.indexes["ih"].structure.check_invariants()
    assert t.indexes["ig"].structure.entry_count == t.indexes["ih"].structure.entry_count == len(model)
    db.close()


def _ids(db: Database) -> list[int]:
    """Todas las PK recorriendo las páginas de resultados del backend."""
    first = db.execute("SELECT id FROM t", page_size=1000)
    ids = [r[0] for r in first["rows"]]
    for page in range(2, -(-first["total_rows"] // 1000) + 1):
        ids += [r[0] for r in db.execute("SELECT id FROM t", page=page, page_size=1000)["rows"]]
    return ids
