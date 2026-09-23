import math
import random

import pytest

from backend.files.common import DuplicateKeyError, RecordNotFoundError
from backend.files.sequential_file import IN_MEMORY_SORT_PAGES, SequentialFile
from backend.storage.disk_manager import DiskCounter
from backend.storage.page import NULL_PAGE
from backend.storage.record import Column, ColumnType, Schema
from tests.conftest import make_row


def _create(tmp_path, schema, counter, page_size=1024, **kw):
    return SequentialFile.create(tmp_path / "t.seq", tmp_path / "t.ovf", schema, page_size, counter, **kw)


def _insert_all(sf, keys, *, check_unique=True):
    for k in keys:
        sf.insert(make_row(k), check_unique=check_unique)
        if sf.needs_reorganization():
            sf.reorganize()


def _main_keys(sf):
    """Claves (activas) de cada página principal, leyendo el archivo directamente."""
    out = []
    for pid in range(1, sf.main_pages + 1):
        page = sf._load_main(pid)
        out.append([sf.key_of(rec) for _, rec in page.records()])
    return out


def test_insercion_aleatoria_con_reorganizacion(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter)
    keys = list(range(1, 3001))
    random.Random(1).shuffle(keys)
    _insert_all(sf, keys)
    assert sf.record_count == 3000
    assert sf.reorganizations > 0
    assert [v[0] for _, v in sf.scan()] == list(range(1, 3001))  # orden de PK
    for k in random.Random(2).sample(keys, 200):
        assert [v for _, v in sf.find(k)] == [make_row(k)]
    assert list(sf.find(0)) == [] and list(sf.find(3001)) == []
    sf.close()


def test_insercion_ordenada_no_genera_overflow(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter, auto_reorganize=False)
    counter.reset()
    for k in range(1, 161):
        sf.insert(make_row(k))
    assert sf.overflow_pages == 0
    assert sf.main_pages == 10  # 160 / 16
    assert sf.reorganizations == 0


def test_sin_reorganizacion_el_overflow_crece(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter, auto_reorganize=False)
    keys = list(range(1, 801))
    random.Random(3).shuffle(keys)
    _insert_all(sf, keys)
    assert not sf.needs_reorganization()
    assert sf.overflow_pages > 10 * sf.main_pages
    assert sorted(v[0] for _, v in sf.scan()) == list(range(1, 801))
    for k in (1, 400, 800):
        assert [v[0] for _, v in sf.find(k)] == [k]


def test_reorganize_conserva_registros_vacia_overflow_y_respeta_fill_factor(
    tmp_path, counter, empleados_schema
):
    sf = _create(tmp_path, empleados_schema, counter, auto_reorganize=False, fill_factor=0.75)
    keys = list(range(1, 1001))
    random.Random(4).shuffle(keys)
    _insert_all(sf, keys)
    rows_before = sorted(v for _, v in sf.scan())
    stats = sf.reorganize()
    assert stats.records == 1000 and stats.overflow_pages_before > 0
    assert sf.overflow_pages == 0
    per_page = sf.records_per_page_after_reorg
    assert per_page == int(16 * 0.75) == 12
    pages = _main_keys(sf)
    assert all(len(p) == per_page for p in pages[:-1]) and 1 <= len(pages[-1]) <= per_page
    assert sf.main_pages == math.ceil(1000 / per_page)
    flat = [k for p in pages for k in p]
    assert flat == sorted(flat)  # slot 0 de cada página = clave frontera
    assert sorted(v for _, v in sf.scan()) == rows_before
    # los punteros next/prev enlazan las páginas principales
    first, last = sf._load_main(1), sf._load_main(sf.main_pages)
    assert first.header.prev_page_id == NULL_PAGE and first.header.next_page_id == 2
    assert last.header.next_page_id == NULL_PAGE
    sf.close()


def test_busqueda_binaria_cuesta_log2_M(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter, auto_reorganize=False)
    for k in range(1, 2001):
        sf.insert(make_row(k), check_unique=False)
    sf.reorganize()
    m = sf.main_pages
    for k in random.Random(5).sample(range(1, 2001), 100):
        counter.reset()
        assert [v[0] for _, v in sf.find(k)] == [k]
        reads, writes = counter.snapshot()
        assert writes == 0
        assert reads <= math.ceil(math.log2(m)) + 1


def test_range_scan(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter)
    keys = list(range(0, 2000, 2))  # pares
    random.Random(6).shuffle(keys)
    _insert_all(sf, keys)
    brute = sorted(keys)
    cases = [(100, 200, True, True), (101, 199, True, True), (100, 200, False, False),
             (None, 50, True, True), (1900, None, True, True), (None, None, True, True),
             (5000, 6000, True, True), (-10, -1, True, True)]
    for lo, hi, li, hi_inc in cases:
        got = [v[0] for _, v in sf.range_scan(lo, hi, low_inclusive=li, high_inclusive=hi_inc)]
        exp = [k for k in brute
               if (lo is None or k > lo or (li and k == lo)) and (hi is None or k < hi or (hi_inc and k == hi))]
        assert got == exp, (lo, hi)


def test_range_scan_costo_acotado(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter, auto_reorganize=False)
    for k in range(1, 5001):
        sf.insert(make_row(k), check_unique=False)
    sf.reorganize()
    counter.reset()
    got = [v[0] for _, v in sf.range_scan(1000, 1119)]
    assert got == list(range(1000, 1120))
    pages_in_range = math.ceil(120 / sf.records_per_page_after_reorg) + 1
    assert counter.disk_reads <= math.ceil(math.log2(sf.main_pages)) + pages_in_range + 1


def test_pk_duplicada(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter, auto_reorganize=False)
    for k in range(1, 60):
        sf.insert(make_row(k * 10))
    with pytest.raises(DuplicateKeyError):
        sf.insert(make_row(10))  # en la página principal
    sf.insert(make_row(15))  # va al overflow
    with pytest.raises(DuplicateKeyError):
        sf.insert(make_row(15))
    assert sf.record_count == 60


def test_borrado_principal_y_overflow(tmp_path, counter, empleados_schema):
    sf = _create(tmp_path, empleados_schema, counter, auto_reorganize=False)
    for k in range(0, 320, 10):
        sf.insert(make_row(k))
    sf.reorganize()
    rid_main = next(sf.find(0))[0]
    for k in (5, 6, 7, 8):  # 12 registros por página tras reorganizar: quedan 4 slots vírgenes
        sf.insert(make_row(k))
    assert next(sf.find(8))[0].page_id == 1
    sf.insert(make_row(9))  # la página 1 ya no tiene slots vírgenes -> overflow
    rid_ovf = next(sf.find(9))[0]
    assert rid_ovf.page_id < 0 and rid_main.page_id == 1
    counter.reset()
    assert sf.delete(rid_ovf) == make_row(9)
    assert counter.snapshot() == (1, 1)
    assert sf.delete(rid_main) == make_row(0)
    with pytest.raises(RecordNotFoundError):
        sf.read(rid_main)
    # el slot 0 queda como lápida: la frontera no cambia y no se reutiliza
    page1 = sf._load_main(1)
    assert sf.key_of(page1.buf, sf.layout.slots_offset) == 0
    assert list(sf.find(0)) == [] and [v[0] for _, v in sf.find(5)] == [5]
    deleted = list(sf.delete_where(lambda buf, off: sf.key_of(buf, off) % 20 == 0))
    assert sorted(v[0] for _, v in deleted) == list(range(20, 320, 20))
    assert sorted(v[0] for _, v in sf.scan()) == sorted(
        [k for k in range(10, 320, 10) if k % 20] + [5, 6, 7, 8]
    )
    assert sf.record_count == 16 + 4


def test_persistencia(tmp_path, empleados_schema):
    sf = _create(tmp_path, empleados_schema, DiskCounter())
    keys = list(range(1, 1500))
    random.Random(8).shuffle(keys)
    _insert_all(sf, keys)
    before = [v for _, v in sf.scan()]
    info = (sf.main_pages, sf.overflow_pages, sf.reorganizations, sf.fill_factor)
    sf.close()
    again = SequentialFile.open(tmp_path / "t.seq", tmp_path / "t.ovf", empleados_schema, 1024, DiskCounter())
    assert (again.main_pages, again.overflow_pages, again.reorganizations, again.fill_factor) == info
    assert again.record_count == 1499
    assert [v for _, v in again.scan()] == before
    again.close()


def test_ordenamiento_externo_en_cadena_larga(tmp_path, counter, empleados_schema):
    # page_size 512 -> 7 registros por página: 1500 registros aleatorios sin
    # reorganizar forman una cadena de overflow de más de 64 páginas.
    sf = _create(tmp_path, empleados_schema, counter, page_size=512, auto_reorganize=False)
    keys = list(range(1, 1501))
    random.Random(9).shuffle(keys)
    for k in keys:
        sf.insert(make_row(k), check_unique=False)
    assert sf.overflow_pages > IN_MEMORY_SORT_PAGES
    stats = sf.reorganize()
    assert stats.external_sorts >= 1
    assert [v[0] for _, v in sf.scan()] == list(range(1, 1501))
    assert not (tmp_path / "t.seq.run").exists() and not (tmp_path / "t.seq.tmp").exists()


def test_pk_char(tmp_path, counter):
    schema = Schema([Column("code", ColumnType.CHAR, 8, primary_key=True), Column("v", ColumnType.INT)])
    sf = SequentialFile.create(tmp_path / "c.seq", tmp_path / "c.ovf", schema, 512, counter)
    words = [f"w{i:05d}" for i in range(400)]
    random.Random(10).shuffle(words)
    for i, w in enumerate(words):
        sf.insert((w, i))
        if sf.needs_reorganization():
            sf.reorganize()
    assert [v[0] for _, v in sf.scan()] == sorted(words)
    assert [v[0] for _, v in sf.find("w00123")] == ["w00123"]
    assert [v[0] for _, v in sf.range_scan("w00010", "w00012")] == ["w00010", "w00011", "w00012"]
