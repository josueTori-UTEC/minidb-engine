import pytest

from backend.sql import ast
from backend.sql.lexer import SQLSyntaxError
from backend.sql.parser import parse, parse_one
from backend.storage.catalog import IndexKind, Organization
from backend.storage.record import ColumnType


def test_create_table_del_enunciado():
    stmt = parse_one(
        "CREATE TABLE empleados (id INT PRIMARY KEY, nombre CHAR(30), dept VARCHAR(20), salario FLOAT) USING SEQUENTIAL;"
    )
    assert isinstance(stmt, ast.CreateTable)
    assert stmt.name == "empleados" and stmt.organization == Organization.SEQUENTIAL
    assert [(c.name, c.type, c.size, c.primary_key) for c in stmt.columns] == [
        ("id", ColumnType.INT, None, True),
        ("nombre", ColumnType.CHAR, 30, False),
        ("dept", ColumnType.CHAR, 20, False),  # VARCHAR es alias de CHAR
        ("salario", ColumnType.FLOAT, None, False),
    ]
    assert parse_one("create table t (a integer)").organization == Organization.HEAP


def test_insert_varias_filas_y_literales():
    stmt = parse_one("INSERT INTO empleados VALUES (101, 'Ada Lovelace', 'Analytics', 5200.0), (-2, 'B', 'C', +3)")
    assert isinstance(stmt, ast.Insert)
    assert stmt.rows == [[101, "Ada Lovelace", "Analytics", 5200.0], [-2, "B", "C", 3]]


def test_create_index():
    stmt = parse_one("CREATE INDEX idx_emp_id ON empleados(id) USING HASH")
    assert (stmt.name, stmt.table, stmt.column, stmt.method) == ("idx_emp_id", "empleados", "id", IndexKind.HASH)
    assert parse_one("CREATE INDEX i ON t (c)").method == IndexKind.BTREE


def test_select_y_where():
    stmt = parse_one("SELECT * FROM empleados WHERE id >= 100 AND id <= 500")
    assert stmt.columns is None
    assert stmt.where == [ast.Comparison("id", ">=", 100), ast.Comparison("id", "<=", 500)]
    stmt = parse_one("SELECT nombre, salario FROM e WHERE id BETWEEN 1 AND 9 AND dept = 'Ventas' AND 5 < salario")
    assert stmt.columns == ["nombre", "salario"]
    assert stmt.where == [ast.Between("id", 1, 9), ast.Comparison("dept", "=", "Ventas"), ast.Comparison("salario", ">", 5)]
    assert parse_one("select * from e where a <> 3").where == [ast.Comparison("a", "!=", 3)]


def test_delete_drop_copy_explain():
    assert parse_one("DELETE FROM e WHERE id = 101").where == [ast.Comparison("id", "=", 101)]
    assert parse_one("DELETE FROM e").where == []
    assert isinstance(parse_one("DROP TABLE e"), ast.DropTable)
    assert parse_one("DROP INDEX idx").name == "idx"
    copy = parse_one("COPY empleados FROM 'empleados.csv'")
    assert (copy.table, copy.path) == ("empleados", "empleados.csv")
    explain = parse_one("EXPLAIN SELECT * FROM e WHERE id = 1")
    assert isinstance(explain, ast.Explain) and isinstance(explain.statement, ast.Select)


def test_script_con_varias_sentencias_y_texto_de_cada_una():
    stmts = parse("CREATE TABLE t (a INT);\n\nINSERT INTO t VALUES (1);;SELECT * FROM t")
    assert [s.kind for s in stmts] == ["CREATE TABLE", "INSERT", "SELECT"]
    assert stmts[1].sql == "INSERT INTO t VALUES (1)"
    assert stmts[2].position.line == 3


@pytest.mark.parametrize(
    "sql, fragment, column",
    [
        ("SELECT * FORM empleados", "se esperaba FROM pero se encontró 'FORM'", 10),
        ("SELEC * FROM t", "sentencia no reconocida", 1),
        ("CREATE TABLE t (id INT, )", "se esperaba nombre de columna", 25),
        ("CREATE TABLE t (id TEXT)", "se esperaba un tipo", 20),
        ("CREATE TABLE t (id INT) USING ISAM", "HEAP o SEQUENTIAL", 31),
        ("INSERT INTO t VALUES (1, 2", "se esperaba ',' o ')'", 27),
        ("SELECT * FROM t WHERE id = ", "se esperaba un literal", 28),
        ("SELECT * FROM t WHERE id = 1 OR id = 2", "se esperaba ';' o el fin de la consulta", 30),
        ("SELECT * FROM select", "palabra reservada", 15),
        ("CREATE INDEX i ON t (c) USING RTREE", "BTREE o HASH", 31),
    ],
)
def test_errores_de_sintaxis_con_posicion(sql, fragment, column):
    with pytest.raises(SQLSyntaxError) as err:
        parse(sql)
    assert fragment in err.value.message
    assert err.value.position.column == column
