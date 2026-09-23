import random
import zlib

import pytest

from backend.files.common import DuplicateKeyError
from backend.indexes.extendible_hash import ExtendibleHash, bucket_capacity, dir_entries_per_page
from backend.storage.disk_manager import DiskCounter
from backend.storage.record import Column, ColumnType
from backend.storage.rid import RID

INT_COL = Column("id", ColumnType.INT)


def _hash(tmp_path, counter, page_size=1024, column=INT_COL, **kw):
    return ExtendibleHash.create(tmp_path / "t.hsh", column, page_size, counter, **kw)


def test_geometria():
    assert [bucket_capacity(b, 4) for b in (1024, 2048, 4096, 8192)] == [100, 202, 407, 816]
    assert [dir_entries_per_page(b) for b in (1024, 2048, 4096, 8192)] == [250, 506, 1018, 2042]


def test_hash_determinista():
    # crc32 no depende de PYTHONHASHSEED: el mismo valor en cualquier proceso
    assert ExtendibleHash.hash_bytes(b"\x65\x00\x00\x00") == zlib.crc32(b"e\x00\x00\x00") == 767_350_829


def test_todas_las_claves_se_encuentran_con_splits_de_directorio(tmp_path, counter):
    h = _hash(tmp_path, counter, page_size=512)  # 48 pares por bucket, 122 punteros por página
    keys = random.Random(1).sample(range(10**7), 20_000)
    for k in keys:
        h.insert(k, RID(k % 997 + 1, k % 13))
    stats = h.check_invariants()
    assert stats["entries"] == 20_000
    assert h.global_depth >= 9 and h.dir_pages > 1 and len(h.segments) > 1
    for k in keys:
        assert h.search(k) == [RID(k % 997 + 1, k % 13)]
    assert h.search(-5) == [] and h.search(10.5) == []


def test_costo_de_busqueda_2_lecturas(tmp_path, counter):
    h = _hash(tmp_path, counter, page_size=4096)
    keys = list(range(50_000))
    random.Random(2).shuffle(keys)
    for k in keys:
        h.insert(k, RID(1, 0))
    for k in keys[:500]:
        counter.reset()
        h.search(k)
        assert counter.snapshot() == (2, 0)  # directorio + bucket


def test_insert_simple_cuesta_2R_1W(tmp_path, counter):
    h = _hash(tmp_path, counter, page_size=4096)
    counter.reset()
    h.insert(42, RID(1, 0))
    assert counter.snapshot() == (2, 1)


def test_duplicados_generan_overflow(tmp_path, counter):
    col = Column("dept", ColumnType.CHAR, 20)
    h = _hash(tmp_path, counter, column=col)  # 1024 -> 38 pares por bucket
    depts = [f"Dept {i}" for i in range(5)]
    expected = {d: [] for d in depts}
    for i in range(3_000):
        d = depts[i % 5]
        h.insert(d, RID(i + 1, 0))
        expected[d].append(RID(i + 1, 0))
    h.check_invariants()
    for d in depts:
        assert sorted(h.search(d)) == expected[d]
    assert h.bucket_pages > 5  # hubo buckets de overflow


def test_una_sola_clave_repetida(tmp_path, counter):
    h = _hash(tmp_path, counter)
    for i in range(1_000):
        h.insert(7, RID(i + 1, 0))
    h.check_invariants()
    assert h.global_depth == 0  # dividir no ayuda: solo overflow
    assert len(h.search(7)) == 1_000


def test_profundidad_maxima(tmp_path, counter):
    h = _hash(tmp_path, counter, max_depth=3)
    keys = list(range(2_000))
    for k in keys:
        h.insert(k, RID(k + 1, 0))
    assert h.global_depth == 3
    h.check_invariants()
    for k in keys:
        assert h.search(k) == [RID(k + 1, 0)]


def test_unico_y_borrado(tmp_path, counter):
    h = _hash(tmp_path, counter, unique=True)
    keys = list(range(3_000))
    random.Random(3).shuffle(keys)
    for k in keys:
        h.insert(k, RID(k + 1, 0))
    with pytest.raises(DuplicateKeyError):
        h.insert(keys[10], RID(9, 9))
    assert h.entry_count == 3_000
    for k in keys[:1_500]:
        counter.reset()
        assert h.delete(k, RID(k + 1, 0))
        assert counter.snapshot() == (2, 1)
    assert not h.delete(keys[0], RID(keys[0] + 1, 0))
    h.check_invariants()
    assert all(h.search(k) == [] for k in keys[:1_500])
    assert all(h.search(k) == [RID(k + 1, 0)] for k in keys[1_500:])
    h.insert(keys[0], RID(5, 5))  # se puede reinsertar tras borrar
    assert h.search(keys[0]) == [RID(5, 5)]


def test_persistencia(tmp_path):
    path = tmp_path / "p.hsh"
    h = ExtendibleHash.create(path, INT_COL, 1024, DiskCounter())
    keys = random.Random(4).sample(range(10**6), 8_000)
    for k in keys:
        h.insert(k, RID(k % 100 + 1, 1))
    gd, segments = h.global_depth, list(h.segments)
    h.close()
    again = ExtendibleHash.open(path, INT_COL, 1024, DiskCounter())
    assert again.global_depth == gd and again.segments == segments
    again.check_invariants()
    assert all(again.search(k) == [RID(k % 100 + 1, 1)] for k in keys[:500])
    again.close()


def test_float_cero_negativo(tmp_path, counter):
    h = _hash(tmp_path, counter, column=Column("x", ColumnType.FLOAT))
    h.insert(0.0, RID(1, 0))
    h.insert(2.5, RID(1, 1))
    assert h.search(-0.0) == [RID(1, 0)]
    assert h.search(2.5) == [RID(1, 1)]
