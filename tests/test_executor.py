import math
import random

import pytest

from backend.database import Database
from backend.sql.errors import SemanticError
from backend.sql.lexer import SQLSyntaxError

CREATE = "CREATE TABLE empleados (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING {org}"
DEPTS = ["Ventas", "Analytics", "RRHH", "TI", "Finanzas"]


def _values(i):
    return f"({i}, 'Empleado {i}', '{DEPTS[i % 5]}', {1000 + (i * 37) % 9000}.5)"


def _load(db, n, org="HEAP", table="empleados", seed=1):
    db.execute(CREATE.format(org=org).replace("empleados", table))
    ids = list(range(1, n + 1))
    random.Random(seed).shuffle(ids)
    for i in range(0, n, 500):
        db.execute(f"INSERT INTO {table} VALUES " + ", ".join(_values(k) for k in ids[i : i + 500]))
    return ids


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path, page_size=1024)
    yield d
    d.close()


def test_select_por_pk_con_y_sin_indice(db):
    _load(db, 3000)
    res = db.execute("SELECT * FROM empleados WHERE id = 101")
    assert res["rows"] == [[101, "Empleado 101", "Analytics", 1000 + (101 * 37) % 9000 + 0.5]]
    assert res["plan"]["type"] == "SeqScan"
    pages = db.tables["empleados"].file.page_count
    assert res["metrics"]["disk_reads"] == pages == res["plan"]["estimated_io"]  # full scan = P lecturas

    msg = db.execute("CREATE INDEX idx_emp_id ON empleados (id) USING BTREE")["message"]
    assert "único" in msg
    res = db.execute("SELECT * FROM empleados WHERE id = 101")
    assert res["plan"]["type"] == "IndexScan" and res["plan"]["structure"] == "BTREE"
    h = db.tables["empleados"].indexes["idx_emp_id"].structure.height
    assert res["metrics"]["disk_reads"] == h + 1 == res["plan"]["estimated_io"]
    assert res["metrics"]["disk_writes"] == 0

    db.execute("CREATE INDEX idx_emp_hash ON empleados (id) USING HASH")
    db.execute("DROP INDEX idx_emp_id")
    res = db.execute("SELECT nombre FROM empleados WHERE id = 2999")
    assert res["plan"]["structure"] == "HASH" and res["rows"] == [["Empleado 2999"]]
    assert res["metrics"]["disk_reads"] == 3  # directorio + bucket + registro


def test_rango_con_btree_y_filtro_residual(db):
    _load(db, 2000)
    db.execute("CREATE INDEX idx_id ON empleados (id)")
    res = db.execute("SELECT id, dept FROM empleados WHERE id >= 100 AND id <= 500 AND dept = 'Ventas'", page_size=1000)
    assert res["plan"]["type"] == "IndexRangeScan"
    assert res["plan"]["filter"] == "dept = 'Ventas'"
    assert [r[0] for r in res["rows"]] == [i for i in range(100, 501) if i % 5 == 0]
    assert res["total_rows"] == 81
    # BETWEEN y cotas exclusivas
    assert db.execute("SELECT id FROM empleados WHERE id BETWEEN 10 AND 12")["rows"] == [[10], [11], [12]]
    assert db.execute("SELECT id FROM empleados WHERE id > 10 AND id < 13")["rows"] == [[11], [12]]
    assert db.execute("SELECT id FROM empleados WHERE 1998 < id")["rows"] == [[1999], [2000]]


def test_modo_costo_prefiere_full_scan_en_rangos_grandes(db):
    _load(db, 3000)
    db.execute("CREATE INDEX idx_id ON empleados (id)")
    rules = db.execute("SELECT * FROM empleados WHERE id >= 1 AND id <= 2000", page_size=1)
    cost = db.execute("SELECT * FROM empleados WHERE id >= 1 AND id <= 2000", page_size=1, planner="cost")
    assert rules["plan"]["type"] == "IndexRangeScan"  # regla del enunciado
    assert cost["plan"]["type"] == "SeqScan"  # optimizador por costos
    assert rules["total_rows"] == cost["total_rows"] == 2000
    assert cost["metrics"]["disk_reads"] < rules["metrics"]["disk_reads"]


def test_sequential_busqueda_binaria_y_rango(db):
    _load(db, 3000, org="SEQUENTIAL")
    t = db.tables["empleados"]
    res = db.execute("SELECT * FROM empleados WHERE id = 1234")
    assert res["plan"]["type"] == "BinarySearch" and res["rows"][0][0] == 1234
    assert res["metrics"]["disk_reads"] <= math.ceil(math.log2(t.file.main_pages)) + 1 + t.file.overflow_pages
    res = db.execute("SELECT id FROM empleados WHERE id BETWEEN 500 AND 700", page_size=500)
    assert res["plan"]["type"] == "SeqFileRangeScan"
    assert [r[0] for r in res["rows"]] == list(range(500, 701))  # sale ordenado por PK
    full = db.execute("SELECT id FROM empleados", page_size=5000)
    assert [r[0] for r in full["rows"]] == list(range(1, 3001))


def test_delete_con_indices(db):
    _load(db, 1500)
    db.execute("CREATE INDEX idx_id ON empleados (id) USING HASH")
    db.execute("CREATE INDEX idx_dept ON empleados (dept) USING BTREE")
    res = db.execute("DELETE FROM empleados WHERE id = 77")
    assert res["message"] == "1 fila(s) eliminada(s)" and res["plan"]["type"] == "IndexScan"
    assert db.execute("SELECT * FROM empleados WHERE id = 77")["total_rows"] == 0
    res = db.execute("DELETE FROM empleados WHERE salario > 5000")  # sin índice -> SeqScan
    assert res["plan"]["type"] == "SeqScan"
    remaining = db.execute("SELECT id, salario FROM empleados", page_size=2000)["rows"]
    assert all(s <= 5000 for _, s in remaining)
    # los índices quedaron consistentes con la tabla
    t = db.tables["empleados"]
    assert t.indexes["idx_id"].structure.entry_count == t.file.record_count == len(remaining)
    by_dept = db.execute("SELECT id FROM empleados WHERE dept = 'TI'", page_size=2000)
    assert by_dept["plan"]["type"] == "IndexScan"
    assert sorted(r[0] for r in by_dept["rows"]) == sorted(i for i, _ in remaining if i % 5 == 3)


def test_unicidad_de_pk(db):
    db.execute(CREATE.format(org="SEQUENTIAL"))
    db.execute(f"INSERT INTO empleados VALUES {_values(1)}, {_values(2)}")
    with pytest.raises(SemanticError, match="duplicada"):
        db.execute(f"INSERT INTO empleados VALUES {_values(3)}, {_values(1)}")
    assert db.execute("SELECT * FROM empleados")["total_rows"] == 3  # sin transacciones: la fila 3 quedó
    db.execute(CREATE.format(org="HEAP").replace("empleados", "heap_t"))
    db.execute(f"INSERT INTO heap_t VALUES {_values(1)}, {_values(1)}")  # HEAP sin índice: no valida
    with pytest.raises(SemanticError, match="repetidos"):
        db.execute("CREATE INDEX i ON heap_t (id)")
    db.execute("DELETE FROM heap_t WHERE id = 1")
    db.execute("CREATE INDEX i ON heap_t (id) USING HASH")
    db.execute(f"INSERT INTO heap_t VALUES {_values(5)}")
    with pytest.raises(SemanticError, match="duplicada"):
        db.execute(f"INSERT INTO heap_t VALUES {_values(5)}")
    assert db.execute("SELECT * FROM heap_t")["total_rows"] == 1  # el insert fallido se deshizo


def test_costo_exacto_de_insert(db):
    db.execute(CREATE.format(org="HEAP"))
    db.execute(f"INSERT INTO empleados VALUES {_values(1)}")
    res = db.execute(f"INSERT INTO empleados VALUES {_values(2)}")
    assert (res["metrics"]["disk_reads"], res["metrics"]["disk_writes"]) == (1, 1)
    db.execute("CREATE INDEX idx ON empleados (id)")
    res = db.execute(f"INSERT INTO empleados VALUES {_values(3)}")
    h = db.tables["empleados"].indexes["idx"].structure.height
    assert (res["metrics"]["disk_reads"], res["metrics"]["disk_writes"]) == (1 + h, 2)
    assert res["plan"]["type"] == "Insert" and res["plan"]["estimated_io"] == 2 + h + 1


def test_paginacion(db):
    _load(db, 250, org="SEQUENTIAL")
    res = db.execute("SELECT id FROM empleados WHERE id <= 120", page=3, page_size=50)
    assert res["total_rows"] == 120 and [r[0] for r in res["rows"]] == list(range(101, 121))
    assert db.execute("SELECT id FROM empleados", page=99, page_size=50)["rows"] == []


def test_copy_desde_csv(db, tmp_path):
    csv_path = tmp_path / "empleados.csv"
    lines = ["id,nombre,dept,salario"] + [f"{i},Nombre {i},Dept {i % 3},{i * 1.5}" for i in range(1, 1001)]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    db.execute(CREATE.format(org="SEQUENTIAL"))
    res = db.execute("COPY empleados FROM 'empleados.csv'")
    assert res["statement"] == "COPY" and "1000 fila(s)" in res["message"]
    assert db.execute("SELECT * FROM empleados WHERE id = 500")["rows"] == [[500, "Nombre 500", "Dept 2", 750.0]]
    with pytest.raises(SemanticError, match="carpeta de datos"):
        db.execute("COPY empleados FROM '../../etc/passwd'")
    with pytest.raises(SemanticError, match="no existe el archivo"):
        db.execute("COPY empleados FROM 'nada.csv'")


def test_persistencia_al_reiniciar(tmp_path):
    db = Database(tmp_path, page_size=2048)
    _load(db, 1200, org="SEQUENTIAL")
    db.execute("CREATE INDEX idx_h ON empleados (id) USING HASH")
    db.execute(CREATE.format(org="HEAP").replace("empleados", "h"))
    db.execute(f"INSERT INTO h VALUES {_values(9)}")
    before = db.execute("SELECT * FROM empleados WHERE id BETWEEN 100 AND 140", page_size=100)["rows"]
    db.close()
    again = Database(tmp_path)
    assert set(again.tables) == {"empleados", "h"}
    assert again.execute("SELECT * FROM empleados WHERE id BETWEEN 100 AND 140", page_size=100)["rows"] == before
    res = again.execute("SELECT * FROM empleados WHERE id = 1000")
    assert res["plan"]["structure"] == "HASH" and res["rows"][0][0] == 1000
    info = {t["name"]: t for t in again.tables_info()}
    assert info["empleados"]["record_count"] == 1200 and info["h"]["record_count"] == 1
    assert info["empleados"]["page_size"] == 2048
    again.close()


def test_reorganize_reconstruye_indices(db):
    _load(db, 2000, org="SEQUENTIAL")
    db.execute("CREATE INDEX idx_id ON empleados (id)")
    db.execute("CREATE INDEX idx_dept ON empleados (dept) USING HASH")
    for k in range(2001, 2400):
        db.execute(f"INSERT INTO empleados VALUES {_values(k)}")
    out = db.reorganize("empleados")
    assert out["after"]["overflow_pages"] == 0 and out["after"]["record_count"] == 2399
    assert out["metrics"]["disk_writes"] > 0
    t = db.tables["empleados"]
    t.indexes["idx_id"].structure.check_invariants()
    t.indexes["idx_dept"].structure.check_invariants()
    assert db.execute("SELECT id FROM empleados WHERE id = 2222")["rows"] == [[2222]]
    assert db.execute("SELECT id FROM empleados WHERE dept = 'RRHH'", page_size=5000)["total_rows"] == 480
    with pytest.raises(SemanticError, match="HEAP"):
        db.execute(CREATE.format(org="HEAP").replace("empleados", "hh"))
        db.reorganize("hh")


def test_explain_y_script(db):
    res = db.execute(CREATE.format(org="HEAP") + f"; INSERT INTO empleados VALUES {_values(1)}; EXPLAIN SELECT * FROM empleados WHERE id = 1;")
    assert res["statement"] == "EXPLAIN" and res["plan"]["type"] == "SeqScan"
    assert [s["statement"] for s in res["statements"]] == ["CREATE TABLE", "INSERT", "EXPLAIN"]
    assert res["metrics"]["disk_reads"] == 0  # EXPLAIN no ejecuta la consulta


def test_errores(db):
    with pytest.raises(SQLSyntaxError) as err:
        db.execute("SELECT * FORM t")
    assert err.value.position.column == 10
    db.execute(CREATE.format(org="HEAP"))
    for sql, fragment in [
        ("SELECT * FROM nope", "no existe la tabla"),
        ("SELECT x FROM empleados", "no existe la columna"),
        ("SELECT * FROM empleados WHERE id = 'a'", "comparar"),
        ("INSERT INTO empleados VALUES (1, 2, 3, 4)", "CHAR"),
        ("INSERT INTO empleados VALUES (1)", "se esperaban 4 valores"),
        ("CREATE TABLE s (a INT) USING SEQUENTIAL", "PRIMARY KEY"),
        ("CREATE TABLE empleados (a INT)", "ya existe"),
        ("CREATE INDEX i ON empleados (nope)", "no existe la columna"),
        ("DROP INDEX nope", "no existe el índice"),
    ]:
        with pytest.raises(SemanticError, match=fragment):
            db.execute(sql)


def test_drop_table_borra_archivos(db, tmp_path):
    _load(db, 100, org="SEQUENTIAL")
    db.execute("CREATE INDEX i ON empleados (id) USING HASH")
    assert (tmp_path / "empleados.seq").exists() and (tmp_path / "empleados_id.hsh").exists()
    db.execute("DROP TABLE empleados")
    assert not any((tmp_path / f).exists() for f in ("empleados.seq", "empleados.ovf", "empleados_id.hsh"))
    assert db.tables_info() == []
