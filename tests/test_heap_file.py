import random

import pytest

from backend.files.common import RecordNotFoundError
from backend.files.heap_file import HeapFile
from backend.storage.disk_manager import DiskCounter
from backend.storage.page import NULL_PAGE
from backend.storage.rid import RID
from tests.conftest import make_row


@pytest.fixture
def heap(tmp_path, counter, empleados_schema):
    h = HeapFile.create(tmp_path / "emp.heap", empleados_schema, 1024, counter)
    yield h
    h.close()


def test_costos_de_insercion_exactos(heap, counter):
    assert heap.capacity == 16
    counter.reset()
    rid = heap.insert(make_row(1))
    assert rid == RID(1, 0)
    assert counter.snapshot() == (0, 1)  # página nueva al final: 1W
    counter.reset()
    assert heap.insert(make_row(2)) == RID(1, 1)
    assert counter.snapshot() == (1, 1)  # página de la free-list: 1R + 1W
    counter.reset()
    heap.flush()  # la cabeza de la free-list cambió al crear la página 1
    assert counter.snapshot() == (0, 1)
    counter.reset()
    heap.insert(make_row(3))
    heap.flush()  # sin cambios estructurales: la página 0 no se escribe
    assert counter.snapshot() == (1, 1)


def test_free_list_y_reutilizacion(heap):
    rids = [heap.insert(make_row(i)) for i in range(40)]  # 16 + 16 + 8
    assert heap.page_count == 3
    assert heap.free_list() == [3]
    assert heap.free_list_head == 3
    heap.delete(rids[5])  # página 1 estaba llena -> vuelve a la cabeza
    assert heap.free_list() == [1, 3]
    assert heap.insert(make_row(99)) == RID(1, 5)  # reutiliza el slot
    assert heap.free_list() == [3]  # la página 1 se llenó y salió de la lista
    assert heap.record_count == 40


def test_scan_cuesta_P_lecturas(heap, counter):
    for i in range(100):
        heap.insert(make_row(i))
    counter.reset()
    rows = list(heap.scan())
    assert len(rows) == 100
    assert counter.snapshot() == (heap.page_count, 0)
    assert sorted(v[0] for _, v in rows) == list(range(100))


def test_read_y_delete(heap, counter):
    rids = [heap.insert(make_row(i)) for i in range(20)]
    counter.reset()
    assert heap.read(rids[17]) == make_row(17)
    assert counter.snapshot() == (1, 0)
    counter.reset()
    assert heap.delete(rids[17]) == make_row(17)
    assert counter.snapshot() == (1, 1)
    with pytest.raises(RecordNotFoundError):
        heap.read(rids[17])
    with pytest.raises(RecordNotFoundError):
        heap.delete(rids[17])
    with pytest.raises(RecordNotFoundError):
        heap.read(RID(50, 0))


def test_delete_many_agrupa_por_pagina(heap, counter):
    rids = [heap.insert(make_row(i)) for i in range(48)]  # 3 páginas llenas
    counter.reset()
    deleted = heap.delete_many([rids[0], rids[1], rids[20], rids[2]])
    assert counter.snapshot() == (2, 2)  # 2 páginas distintas
    assert sorted(v[0] for _, v in deleted) == [0, 1, 2, 20]
    assert heap.record_count == 44
    assert set(heap.free_list()) == {1, 2}


def test_delete_where_en_un_solo_recorrido(heap, empleados_schema, counter):
    for i in range(64):
        heap.insert(make_row(i))
    off = empleados_schema.offsets[0]

    def es_par(buf, pos):
        return int.from_bytes(buf[pos + off : pos + off + 4], "little", signed=True) % 2 == 0

    counter.reset()
    deleted = list(heap.delete_where(es_par))
    assert len(deleted) == 32
    assert counter.snapshot() == (4, 4)  # 4 páginas leídas y reescritas
    assert sorted(v[0] for _, v in heap.scan()) == list(range(1, 64, 2))


def test_persistencia(tmp_path, empleados_schema):
    path = tmp_path / "p.heap"
    rng = random.Random(7)
    h = HeapFile.create(path, empleados_schema, 2048, DiskCounter())
    rids = [h.insert(make_row(i)) for i in range(500)]
    for rid in rng.sample(rids, 100):
        h.delete(rid)
    expected = sorted(v for _, v in h.scan())
    head = h.free_list_head
    h.close()
    again = HeapFile.open(path, empleados_schema, 2048, DiskCounter())
    assert again.record_count == 400
    assert again.free_list_head == head != NULL_PAGE
    assert sorted(v for _, v in again.scan()) == expected
    again.close()
