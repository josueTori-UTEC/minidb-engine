import pytest

from backend.storage.catalog import (
    Catalog,
    CatalogError,
    IndexInfo,
    IndexKind,
    Organization,
    TableInfo,
)
from backend.storage.disk_manager import DiskCounter
from backend.storage.page import PageHeader, PageType
from backend.storage.record import Column, ColumnType


def _empleados(name="empleados", org=Organization.HEAP):
    return TableInfo(
        name=name,
        columns=[
            Column("id", ColumnType.INT, primary_key=True),
            Column("nombre", ColumnType.CHAR, 30),
            Column("dept", ColumnType.CHAR, 20),
            Column("salario", ColumnType.FLOAT),
        ],
        organization=org,
        page_size=4096,
        files=[f"{name}.heap"] if org == Organization.HEAP else [f"{name}.seq", f"{name}.ovf"],
        fill_factor=0.8,
        reorg_ratio=0.1,
        auto_reorganize=False,
    )


def test_catalogo_sobrevive_reinicio(tmp_path, counter):
    cat = Catalog(tmp_path, counter)
    cat.add_table(_empleados())
    cat.add_table(_empleados("emp_seq", Organization.SEQUENTIAL))
    cat.add_index(IndexInfo("idx_emp_id", "empleados", "id", IndexKind.BTREE, "empleados_id.bpt"))
    cat.add_index(IndexInfo("idx_emp_h", "empleados", "id", IndexKind.HASH, "empleados_id.hsh"))

    again = Catalog(tmp_path, DiskCounter())
    assert set(again.tables) == {"empleados", "emp_seq"}
    t = again.get_table("empleados")
    assert t == cat.get_table("empleados")
    assert t.schema.record_size == 62
    assert [i.kind for i in t.indexes] == [IndexKind.BTREE, IndexKind.HASH]
    seq = again.get_table("emp_seq")
    assert seq.organization == Organization.SEQUENTIAL
    assert (seq.fill_factor, seq.auto_reorganize) == (0.8, False)


def test_formato_binario_paginado(tmp_path, counter):
    cat = Catalog(tmp_path, counter)
    cat.add_table(_empleados())
    raw = (tmp_path / "catalog.bin").read_bytes()
    assert len(raw) % 4096 == 0
    assert PageHeader.unpack(raw[:24]).page_type == PageType.FILE_META
    assert PageHeader.unpack(raw[4096:4120]).page_type == PageType.CATALOG
    assert b"empleados" in raw and b"{" not in raw  # no es JSON


def test_catalogo_grande_ocupa_varias_paginas(tmp_path, counter):
    cat = Catalog(tmp_path, counter)
    for i in range(80):
        cat.add_table(_empleados(f"tabla_con_nombre_largo_{i:03d}"))
    assert (tmp_path / "catalog.bin").stat().st_size >= 3 * 4096
    again = Catalog(tmp_path, counter)
    assert len(again.tables) == 80
    for i in range(70):
        again.drop_table(f"tabla_con_nombre_largo_{i:03d}")
    assert (tmp_path / "catalog.bin").stat().st_size == 2 * 4096  # se recorta
    assert len(Catalog(tmp_path, counter).tables) == 10


def test_errores_y_drop(tmp_path, counter):
    cat = Catalog(tmp_path, counter)
    cat.add_table(_empleados())
    with pytest.raises(CatalogError):
        cat.add_table(_empleados())
    with pytest.raises(CatalogError):
        cat.get_table("nope")
    cat.add_index(IndexInfo("i1", "empleados", "id", IndexKind.BTREE, "a.bpt"))
    with pytest.raises(CatalogError):
        cat.add_index(IndexInfo("i1", "empleados", "dept", IndexKind.HASH, "b.hsh"))
    table, idx = cat.find_index("i1")
    assert table.name == "empleados" and idx.column == "id"
    cat.drop_index("i1")
    assert not Catalog(tmp_path, counter).get_table("empleados").indexes
    cat.drop_table("empleados")
    assert not Catalog(tmp_path, counter).tables


def test_guardar_cuenta_escrituras(tmp_path):
    counter = DiskCounter()
    cat = Catalog(tmp_path, counter)
    counter.reset()
    cat.add_table(_empleados())
    assert counter.snapshot() == (0, 2)  # 1 página de datos + página 0
