"""SQL multimodal de punta a punta: tipos, índices, predicados, ranking y planificador híbrido."""

import random

import numpy as np
import pytest

from backend.database import Database
from backend.indexes.rtree import haversine_km
from backend.sql.errors import SemanticError, SQLSyntaxError
from backend.sql.parser import parse_one
from backend.sql import ast
from backend.storage.record import Column, ColumnType, Schema, SchemaError
from backend.tools.dump_page import inspect_page

N = 1200
CATS = ["footwear", "apparel", "bags", "watches"]
WORDS = "zapatilla roja azul cuero deportiva casual formal verano invierno ligera running urbana negra blanca".split()
CENTER = (-77.03, -12.10)
DIM = 12


def _vec(v) -> str:
    return "[" + ", ".join(f"{x:.5f}" for x in v) + "]"


@pytest.fixture(scope="module")
def data():
    rng = np.random.default_rng(1)
    py = random.Random(1)
    centers = rng.normal(size=(4, DIM))
    rows = []
    for i in range(N):
        c = i % 4
        rows.append({
            "id": i, "cat": CATS[c], "precio": round(py.uniform(10, 500), 2),
            "lon": round(CENTER[0] + rng.uniform(-0.1, 0.1), 6), "lat": round(CENTER[1] + rng.uniform(-0.1, 0.1), 6),
            "desc": " ".join(py.choices(WORDS, k=8)),
            "emb": (centers[c] + 0.4 * rng.normal(size=DIM)).astype(np.float32),
        })
    return rows, centers


def _load(db: Database, rows) -> None:
    db.execute(
        "CREATE TABLE prod (id INT PRIMARY KEY, categoria CHAR(12), precio FLOAT, ubicacion POINT, "
        f"descripcion TEXT(120), emb VECTOR({DIM})) USING HEAP"
    )
    values = [
        f"({r['id']}, '{r['cat']}', {r['precio']}, ({r['lon']}, {r['lat']}), '{r['desc']}', {_vec(r['emb'])})"
        for r in rows
    ]
    for start in range(0, len(values), 400):
        db.execute("INSERT INTO prod VALUES " + ", ".join(values[start : start + 400]))


INDEXES = [
    "CREATE INDEX idx_geo ON prod (ubicacion) USING RTREE",
    "CREATE INDEX idx_txt ON prod (descripcion) USING INVERTED WITH (scoring = 'TFIDF_COSINE')",
    "CREATE INDEX idx_vec ON prod (emb) USING IVF WITH (lists = 12, metric = 'COSINE')",
    "CREATE INDEX idx_precio ON prod (precio) USING BTREE",
]


@pytest.fixture(scope="module")
def plain(tmp_path_factory, data):
    """Base sin índices: todo se resuelve con recorridos secuenciales (línea base)."""
    db = Database(tmp_path_factory.mktemp("plain"))
    _load(db, data[0])
    yield db
    db.close()


@pytest.fixture(scope="module")
def indexed(tmp_path_factory, data):
    db = Database(tmp_path_factory.mktemp("indexed"))
    _load(db, data[0])
    for sql in INDEXES:
        db.execute(sql)
    yield db
    db.close()


def _all(db, sql):
    out = db.execute(sql, page_size=1000)
    return out["rows"], out["plan"], out["metrics"]


# ---------------------------------------------------------------------- tipos
def test_tipos_point_y_vector_en_el_registro():
    schema = Schema([
        Column("id", ColumnType.INT, primary_key=True), Column("p", ColumnType.POINT),
        Column("v", ColumnType.VECTOR, 4), Column("t", ColumnType.CHAR, 300),
    ])
    assert schema.record_size == 4 + 16 + 16 + 300
    row = schema.coerce([7, "(-77.03, -12.1)", "[0.5, -1, 2.25, 3]", "texto largo"])
    assert row == (7, (-77.03, -12.1), (0.5, -1.0, 2.25, 3.0), "texto largo")
    assert schema.decode(schema.encode(row)) == row
    assert schema.coerce([1, (1, 2), [1, 2, 3, 4], ""])[1:3] == ((1.0, 2.0), (1.0, 2.0, 3.0, 4.0))
    for bad in ([1, (1, 2, 3), [1, 2, 3, 4], ""], [1, (1, 2), [1, 2, 3], ""], [1, "x", [1, 2, 3, 4], ""]):
        with pytest.raises(SchemaError):
            schema.coerce(bad)
    with pytest.raises(SchemaError):
        Column("v", ColumnType.VECTOR, 4, primary_key=True)
    with pytest.raises(SchemaError):
        schema.columns[1].comparable((1, 2))


def test_parser_de_la_gramatica_extendida():
    s = parse_one("CREATE INDEX idx_vec ON multimedia (embedding) USING IVF WITH (lists = 100, metric = 'COSINE')")
    assert isinstance(s, ast.CreateIndex) and s.method.value == "IVF"
    assert s.options == {"lists": "100", "metric": "COSINE"}
    s = parse_one("SELECT id FROM locales WHERE ubicacion WITHIN_RADIUS((-77.0282, -12.1354), 2.5)")
    assert s.where == [ast.WithinRadius("ubicacion", (-77.0282, -12.1354), 2.5)] and not s.is_relational
    s = parse_one(
        "SELECT id, titulo, COSINE_SCORE() AS relevancia FROM documentos "
        "WHERE cuerpo MATCH 'aprendizaje AND profundo AND NOT basico' ORDER BY relevancia DESC LIMIT 10"
    )
    assert s.items[2] == ast.SelectItem(ast.Expr("score"), "relevancia")
    assert s.order_by == ast.OrderBy(ast.Expr("column", "relevancia"), True) and s.limit == 10
    s = parse_one("SELECT id, embedding <=> '[0.12, -0.34]' AS d FROM m WHERE precio <= 250.0 ORDER BY d ASC LIMIT 5")
    assert s.items[1].expr == ast.Expr("distance", "embedding", "<=>", "[0.12, -0.34]")
    s = parse_one("SELECT * FROM t ORDER BY p <-> (1, -2.5) LIMIT 3")
    assert s.order_by.expr == ast.Expr("distance", "p", "<->", (1.0, -2.5)) and s.columns is None
    assert parse_one("SET ivf.probes = 5") == ast.Set("ivf.probes", 5, sql="SET ivf.probes = 5", position=ast.Position(1, 1, 0))
    assert parse_one("SELECT text, point FROM t").columns == ["text", "point"]  # no son palabras reservadas
    assert parse_one("SELECT * FROM t WHERE id = 3").is_relational
    for bad in ("SELECT * FROM t WHERE p WITHIN_RADIUS((1, 2), -1)", "SELECT * FROM t WHERE a <-> 3",
                "SELECT * FROM t LIMIT x", "CREATE INDEX i ON t (c) USING IVF WITH (lists)"):
        with pytest.raises(SQLSyntaxError):
            parse_one(bad)


# ---------------------------------------------------------------------- espacial
def test_radio_rtree_igual_a_haversine(plain, indexed, data):
    rows, _ = data
    sql = "SELECT id FROM prod WHERE ubicacion WITHIN_RADIUS((-77.03, -12.1), 2.5)"
    expected = sorted(r["id"] for r in rows if haversine_km(*CENTER, r["lon"], r["lat"]) <= 2.5)
    seq_rows, seq_plan, seq_m = _all(plain, sql)
    idx_rows, idx_plan, idx_m = _all(indexed, sql)
    assert sorted(r[0] for r in seq_rows) == sorted(r[0] for r in idx_rows) == expected and expected
    assert seq_plan["type"] == "SeqScan" and idx_plan["type"] == "RTreeRadiusScan" and idx_plan["index"] == "idx_geo"
    assert idx_m["disk_reads"] < seq_m["disk_reads"]
    spatial = idx_plan["spatial"]
    assert spatial["kind"] == "radius" and spatial["nodes_visited"] == idx_m["io"]["index_reads"]
    assert spatial["candidates"] >= len(expected) and spatial["mbrs"]
    assert idx_m["io"]["table_reads"] == spatial["candidates"]  # una lectura por candidato


def test_ventana_y_knn_geografico(plain, indexed, data):
    rows, _ = data
    sql = "SELECT id FROM prod WHERE ubicacion WITHIN_BOX((-77.05, -12.12), (-77.01, -12.08))"
    expected = sorted(r["id"] for r in rows if -77.05 <= r["lon"] <= -77.01 and -12.12 <= r["lat"] <= -12.08)
    assert sorted(r[0] for r in _all(indexed, sql)[0]) == sorted(r[0] for r in _all(plain, sql)[0]) == expected
    sql = "SELECT id, ubicacion <-> (-77.03, -12.1) AS km FROM prod ORDER BY km LIMIT 8"
    by_dist = sorted(rows, key=lambda r: haversine_km(*CENTER, r["lon"], r["lat"]))[:8]
    seq_rows, seq_plan, _ = _all(plain, sql)
    idx_rows, idx_plan, idx_m = _all(indexed, sql)
    assert [r[0] for r in seq_rows] == [r[0] for r in idx_rows] == [r["id"] for r in by_dist]
    assert idx_rows[0][1] == pytest.approx(haversine_km(*CENTER, by_dist[0]["lon"], by_dist[0]["lat"]), abs=1e-5)
    assert idx_plan["type"] == "RTreeKNN" and idx_m["io"]["table_reads"] == 8
    assert idx_m["disk_reads"] < 30 and seq_plan["type"] == "SeqScan"


def test_knn_geografico_con_filtro(indexed, data):
    rows, _ = data
    sql = ("SELECT id, ubicacion <-> (-77.03, -12.1) AS km FROM prod "
           "WHERE precio <= 100 AND categoria = 'bags' ORDER BY km LIMIT 5")
    ok = [r for r in rows if r["precio"] <= 100 and r["cat"] == "bags"]
    expected = [r["id"] for r in sorted(ok, key=lambda r: haversine_km(*CENTER, r["lon"], r["lat"]))[:5]]
    for strategy in ("pre", "post", "auto"):
        indexed.execute(f"SET hybrid.strategy = {strategy}")
        got, plan, _ = _all(indexed, sql)
        assert [r[0] for r in got] == expected, strategy
        if strategy != "auto":
            assert plan["strategy"] == f"{strategy}-filter"
    indexed.execute("SET hybrid.strategy = auto")


# ---------------------------------------------------------------------- texto
def test_match_booleano_con_y_sin_indice(plain, indexed):
    sql = "SELECT id FROM prod WHERE descripcion MATCH 'zapatilla AND roja AND NOT cuero'"
    seq_rows, seq_plan, seq_m = _all(plain, sql)
    idx_rows, idx_plan, idx_m = _all(indexed, sql)
    assert sorted(r[0] for r in seq_rows) == sorted(r[0] for r in idx_rows) and idx_rows
    assert seq_plan["type"] == "SeqScan" and idx_plan["type"] == "InvertedIndexScan"
    text = idx_plan["text"]
    assert text["mode"] == "boolean" and [t["term"] for t in text["terms"]] == ["zapatill", "roj", "cuer"]
    assert text["matched"] == len(idx_rows)
    files = {f["file"]: f["reads"] for f in idx_m["io"]["files"]}
    assert set(files) == {"prod_descripcion.vocab.dic", "prod_descripcion.postings.inv",
                          "prod_descripcion.docs.bin", "prod.heap"}
    assert idx_m["io"]["index_reads"] + idx_m["io"]["table_reads"] == idx_m["disk_reads"]


def test_ranking_cosine_score(plain, indexed):
    sql = ("SELECT id, COSINE_SCORE() AS relevancia FROM prod "
           "WHERE descripcion MATCH 'zapatilla roja deportiva' ORDER BY relevancia DESC LIMIT 10")
    rows, plan, m = _all(indexed, sql)
    scores = [r[1] for r in rows]
    assert len(rows) == 10 and scores == sorted(scores, reverse=True) and 0 < scores[0] <= 1.0
    assert plan["type"] == "InvertedIndexScan" and plan["strategy"] is None and m["io"]["table_reads"] == 10
    with pytest.raises(SemanticError, match="INVERTED"):
        plain.execute(sql)
    with pytest.raises(SemanticError, match="MATCH"):
        indexed.execute("SELECT id, COSINE_SCORE() AS r FROM prod")


def test_ranking_textual_con_filtro_pre_y_post(indexed):
    sql = ("SELECT id, precio, COSINE_SCORE() AS r FROM prod "
           "WHERE descripcion MATCH 'zapatilla roja deportiva' AND precio <= 60 ORDER BY r DESC LIMIT 5")
    results = {}
    for strategy in ("pre", "post"):
        indexed.execute(f"SET hybrid.strategy = {strategy}")
        rows, plan, m = _all(indexed, sql)
        assert plan["strategy"] == f"{strategy}-filter" and all(r[1] <= 60 for r in rows)
        results[strategy] = (rows, m)
    indexed.execute("SET hybrid.strategy = auto")
    assert results["pre"][0] == results["post"][0] and len(results["pre"][0]) == 5
    # el pre-filtro solo trae de la tabla el Top-K; el post-filtro trae candidatos que luego descarta
    assert results["pre"][1]["io"]["table_reads"] == 5 < results["post"][1]["io"]["table_reads"]


# ---------------------------------------------------------------------- vectores
def _exact(rows, q, pred, k):
    def cos(v):
        return 1 - float(v @ q) / (np.linalg.norm(v) * np.linalg.norm(q))
    return [r["id"] for r in sorted((r for r in rows if pred(r)), key=lambda r: cos(r["emb"]))[:k]]


def test_knn_vectorial_exacto_por_recorrido(plain, data):
    rows, centers = data
    q = centers[0].astype(np.float32)
    sql = f"SELECT id, emb <=> '{_vec(q)}' AS d FROM prod ORDER BY d ASC LIMIT 7"
    got, plan, m = _all(plain, sql)
    assert [r[0] for r in got] == _exact(rows, q, lambda r: True, 7)
    assert plan["type"] == "SeqScan" and m["disk_reads"] == plain.tables["prod"].file.page_count
    l2 = _all(plain, f"SELECT id, emb <-> '{_vec(q)}' AS d FROM prod ORDER BY d LIMIT 3")[0]
    expected = sorted(rows, key=lambda r: float(np.linalg.norm(r["emb"] - q)))[:3]
    assert [r[0] for r in l2] == [r["id"] for r in expected]
    assert l2[0][1] == pytest.approx(float(np.linalg.norm(expected[0]["emb"] - q)), abs=1e-4)


def test_ivf_aproximado_y_probes(indexed, data):
    rows, centers = data
    q = centers[1].astype(np.float32)
    sql = f"SELECT id, emb <=> '{_vec(q)}' AS d FROM prod ORDER BY d ASC LIMIT 10"
    truth = set(_exact(rows, q, lambda r: True, 10))
    indexed.execute("SET ivf.probes = 12")  # todas las celdas: exacto
    got, plan, m_all = _all(indexed, sql)
    assert plan["type"] == "IVFScan" and plan["vector"]["probes"] == 12 and plan["vector"]["scanned"] == N
    assert {r[0] for r in got} == truth and [r[1] for r in got] == sorted(r[1] for r in got)
    indexed.execute("SET ivf.probes = 2")
    got2, plan2, m2 = _all(indexed, sql)
    assert len(got2) == 10 and plan2["vector"]["scanned"] < N and m2["disk_reads"] < m_all["disk_reads"]
    assert len({r[0] for r in got2} & truth) >= 6  # aproximado, pero la mayoría coincide
    indexed.execute("SET ivf.probes = 5")
    # la métrica del operador debe coincidir con la del índice (COSINE); <-> usa el recorrido exacto
    assert _all(indexed, f"SELECT id FROM prod ORDER BY emb <-> '{_vec(q)}' LIMIT 3")[1]["type"] == "SeqScan"


def test_consulta_hibrida_pre_y_post_filtering(indexed, data):
    rows, centers = data
    q = centers[0].astype(np.float32)
    base = f"SELECT id, categoria, precio, emb <=> '{_vec(q)}' AS d FROM prod WHERE %s ORDER BY d ASC LIMIT 5"
    wide = base % "categoria = 'footwear' AND precio <= 400.0"
    expected = _exact(rows, q, lambda r: r["cat"] == "footwear" and r["precio"] <= 400.0, 5)
    indexed.execute("SET hybrid.strategy = pre")
    pre_rows, pre_plan, pre_m = _all(indexed, wide)
    indexed.execute("SET hybrid.strategy = post")
    post_rows, post_plan, post_m = _all(indexed, wide)
    indexed.execute("SET hybrid.strategy = auto")
    auto_rows, auto_plan, _ = _all(indexed, wide)
    assert [r[0] for r in pre_rows] == expected  # el pre-filtro es exacto
    assert pre_plan["strategy"] == "pre-filter" and pre_plan["type"] == "IndexRangeScan"
    assert post_plan["strategy"] == "post-filter" and post_plan["type"] == "IVFScan"
    assert all(r[1] == "footwear" and r[2] <= 400 for r in post_rows) and len(post_rows) == 5
    # filtro poco selectivo: el post-filtro lee mucho menos y es el que elige el planificador
    assert post_m["disk_reads"] < pre_m["disk_reads"] and auto_plan["type"] == "IVFScan"
    assert {c["strategy"] for c in auto_plan["candidates"]} >= {"pre-filter", "post-filter"}
    # filtro muy selectivo: el planificador cambia a pre-filtro; el post-filtro se queda corto y avisa
    narrow = base % "precio <= 14.0"
    expected = _exact(rows, q, lambda r: r["precio"] <= 14.0, 5)
    rows_auto, plan_auto, _ = _all(indexed, narrow)
    assert plan_auto["strategy"] == "pre-filter" and [r[0] for r in rows_auto] == expected
    indexed.execute("SET hybrid.strategy = post")
    rows_post, plan_post, _ = _all(indexed, narrow)
    indexed.execute("SET hybrid.strategy = auto")
    assert len(rows_post) < 5 and "post-filtering" in plan_post["warning"]


def test_todo_junto(plain, indexed):
    sql = ("SELECT id FROM prod WHERE ubicacion WITHIN_RADIUS((-77.03, -12.1), 6) AND descripcion MATCH 'roja' "
           "AND precio BETWEEN 50 AND 400 ORDER BY id DESC LIMIT 6")
    assert _all(plain, sql)[0] == _all(indexed, sql)[0] and len(_all(plain, sql)[0]) == 6
    assert indexed.execute("EXPLAIN " + sql)["plan"]["candidates"]


# ---------------------------------------------------------------------- DDL, mantenimiento y errores
def test_validaciones_de_indices_y_parametros(plain):
    for sql, fragment in [
        ("CREATE INDEX x ON prod (precio) USING RTREE", "POINT"),
        ("CREATE INDEX x ON prod (emb) USING BTREE", "IVF"),
        ("CREATE INDEX x ON prod (categoria) USING IVF", "VECTOR"),
        ("CREATE INDEX x ON prod (emb) USING INVERTED", "texto"),
        ("CREATE INDEX x ON prod (emb) USING IVF WITH (metric = 'MANHATTAN')", "métrica"),
        ("CREATE INDEX x ON prod (descripcion) USING INVERTED WITH (lists = 3)", "opción desconocida"),
        ("SELECT * FROM prod WHERE precio MATCH 'a'", "texto"),
        ("SELECT * FROM prod WHERE categoria WITHIN_RADIUS((0, 0), 1)", "POINT"),
        ("SELECT id, precio <-> '[1]' AS d FROM prod", "POINT o VECTOR"),
        ("SELECT id, emb <=> '[1, 2]' AS d FROM prod", "componentes"),
        ("SELECT * FROM prod WHERE emb = 3", "no admite comparaciones"),
        ("SELECT * FROM prod ORDER BY nada", "ORDER BY"),
        ("SET ivf.probes = 0", "ivf.probes"),
        ("SET nada = 1", "desconocido"),
    ]:
        with pytest.raises(SemanticError, match=fragment):
            plain.execute(sql)
    assert not (plain.data_dir / "prod_emb.ivf.dir").exists()  # un CREATE INDEX fallido no deja archivos


def test_mantenimiento_y_persistencia(tmp_path, data):
    rows, centers = data
    db = Database(tmp_path)
    _load(db, rows[:300])
    for sql in INDEXES:
        db.execute(sql)
    q = _vec(centers[2])
    db.execute(f"INSERT INTO prod VALUES (5000, 'bags', 20.0, (-77.03, -12.1), 'mochila turquesa unica', {q})")
    assert db.tables["prod"].indexes["idx_txt"].structure.stale  # el invertido se reconstruye al consultarlo
    out = db.execute("SELECT id, COSINE_SCORE() AS r FROM prod WHERE descripcion MATCH 'turquesa' ORDER BY r DESC LIMIT 3")
    assert out["rows"][0][0] == 5000 and out["plan"]["text"]["rebuilt"]
    assert db.execute(f"SELECT id FROM prod ORDER BY emb <=> '{q}' LIMIT 1")["rows"] == [[5000]]
    assert db.execute("SELECT id FROM prod ORDER BY ubicacion <-> (-77.03, -12.1) LIMIT 1")["rows"] == [[5000]]
    assert db.execute("DELETE FROM prod WHERE ubicacion WITHIN_RADIUS((-77.03, -12.1), 0.01)")["message"].startswith("1 ")
    assert db.execute("SELECT id FROM prod WHERE descripcion MATCH 'turquesa'")["rows"] == []
    assert db.execute(f"SELECT id FROM prod ORDER BY emb <=> '{q}' LIMIT 1")["rows"] != [[5000]]
    expected = db.execute("SELECT id FROM prod WHERE ubicacion WITHIN_RADIUS((-77.03, -12.1), 4)", page_size=1000)["rows"]
    db.close()
    db = Database(tmp_path)
    kinds = {i["name"]: i["type"] for i in db.tables_info()[0]["indexes"]}
    assert kinds == {"idx_geo": "RTREE", "idx_txt": "INVERTED", "idx_vec": "IVF", "idx_precio": "BTREE"}
    assert db.catalog.tables["prod"].index_named("idx_vec").options == {"lists": "12", "metric": "COSINE"}
    again = db.execute("SELECT id FROM prod WHERE ubicacion WITHIN_RADIUS((-77.03, -12.1), 4)", page_size=1000)
    assert again["rows"] == expected and again["plan"]["type"] == "RTreeRadiusScan"
    files = {f["key"]: f["kind"] for f in db.table_files("prod")}
    assert files["idx_txt:postings.inv"] == "INVERTED" and files["idx_vec:ivf.lst"] == "IVF" and files["idx_geo"] == "RTREE"
    for key, expected_kind in [("idx_geo", "hoja"), ("idx_txt:vocab.dic", "diccionario"),
                               ("idx_txt:postings.inv", "posteo"), ("idx_txt:docs.bin", "estadísticas"),
                               ("idx_vec:ivf.dir", "centroides"), ("idx_vec:ivf.lst", "lista invertida")]:
        table, path, _ = db.file_path("prod", key)
        table.sync()
        info = inspect_page(path, 1, table.info.page_size, table.schema)
        assert expected_kind in info["details"]["kind"] and info["details"]["entries"], key
    db.execute("DROP INDEX idx_txt")
    assert not list(tmp_path.glob("prod_descripcion.*"))
    db.execute("DROP TABLE prod")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["catalog.bin"]
    db.close()
