import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app

CREATE = "CREATE TABLE empleados (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING {org};"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        yield c


def _rows(n):
    return ", ".join(f"({i}, 'Emp {i}', 'D{i % 4}', {i * 10.0})" for i in range(1, n + 1))


def test_health_y_cors(client):
    assert client.get("/api/health").json()["status"] == "ok"
    res = client.options(
        "/api/query",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_query_con_plan_y_metricas(client):
    res = client.post("/api/query", json={"sql": CREATE.format(org="HEAP") + f"INSERT INTO empleados VALUES {_rows(300)};"})
    assert res.status_code == 200
    body = res.json()
    assert body["statement"] == "INSERT" and len(body["statements"]) == 2
    client.post("/api/query", json={"sql": "CREATE INDEX idx_emp_id ON empleados (id) USING BTREE"})
    body = client.post("/api/query", json={"sql": "SELECT * FROM empleados WHERE id = 101;"}).json()
    assert body["columns"] == ["id", "nombre", "dept", "salario"]
    assert body["rows"] == [[101, "Emp 101", "D1", 1010.0]]
    assert body["plan"]["type"] == "IndexScan" and body["plan"]["index"] == "idx_emp_id"
    assert body["plan"]["structure"] == "BTREE"
    assert set(body["metrics"]) == {"disk_reads", "disk_writes", "parse_ms", "exec_ms", "total_ms", "io"}
    assert body["metrics"]["disk_reads"] == body["plan"]["estimated_io"]
    assert any(c["chosen"] for c in body["plan"]["candidates"])


def test_paginacion_y_planner_por_costos(client):
    client.post("/api/query", json={"sql": CREATE.format(org="SEQUENTIAL") + f"INSERT INTO empleados VALUES {_rows(500)}"})
    body = client.post("/api/query", json={"sql": "SELECT id FROM empleados", "page": 2, "page_size": 100}).json()
    assert body["total_rows"] == 500 and body["rows"][0] == [101] and len(body["rows"]) == 100
    body = client.post("/api/query", json={"sql": "SELECT id FROM empleados WHERE id = 7", "planner": "cost"}).json()
    assert body["plan"]["type"] == "BinarySearch" and body["plan"]["planner"] == "cost"
    assert client.post("/api/query", json={"sql": "SELECT 1", "page_size": 5000}).status_code == 422


def test_errores_400_con_posicion(client):
    res = client.post("/api/query", json={"sql": "SELECT * FORM empleados"})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["error"] == "SyntaxError"
    assert detail["position"] == {"line": 1, "column": 10, "offset": 9}
    res = client.post("/api/query", json={"sql": "SELECT * FROM nada"})
    assert res.status_code == 400 and res.json()["detail"]["error"] == "SemanticError"
    res = client.post("/api/query", json={"sql": CREATE.format(org="HEAP") + " SELECT * FROM x"})
    detail = res.json()["detail"]
    assert detail["executed"][0]["statement"] == "CREATE TABLE"  # lo previo sí se ejecutó


def test_tables_y_reorganize(client):
    client.post("/api/query", json={"sql": CREATE.format(org="SEQUENTIAL") + f"INSERT INTO empleados VALUES {_rows(400)}"})
    client.post("/api/query", json={"sql": "CREATE INDEX h ON empleados (dept) USING HASH"})
    tables = client.get("/api/tables").json()["tables"]
    t = tables[0]
    assert t["name"] == "empleados" and t["organization"] == "SEQUENTIAL"
    assert t["record_count"] == 400 and t["record_size"] == 62 and t["records_per_page"] == 65
    assert t["columns"][1] == {"name": "nombre", "type": "CHAR", "size": 30, "primary_key": False}
    assert t["indexes"][0]["type"] == "HASH" and t["indexes"][0]["global_depth"] is not None
    res = client.post("/api/tables/reorganize", json={"table": "empleados"})
    assert res.status_code == 200
    body = res.json()
    assert body["after"]["overflow_pages"] == 0 and body["metrics"]["disk_writes"] > 0
    assert client.post("/api/tables/reorganize", json={"table": "nada"}).status_code == 404


def test_inspeccion_de_paginas(client):
    client.post("/api/query", json={"sql": CREATE.format(org="HEAP") + f"INSERT INTO empleados VALUES {_rows(100)}"})
    client.post("/api/query", json={"sql": "CREATE INDEX idx ON empleados (id)"})
    files = client.get("/api/tables/empleados/files").json()["files"]
    assert [f["key"] for f in files] == ["data", "idx"]
    meta = client.get("/api/tables/empleados/pages/0").json()
    assert meta["header"]["page_type_name"] == "FILE_META"
    assert meta["details"]["fields"]["record_count"] == 100
    page = client.get("/api/tables/empleados/pages/1").json()
    assert page["header"]["page_type_name"] == "HEAP_DATA" and page["header"]["record_count"] == 65
    assert page["details"]["records"][0]["values"] == [1, "Emp 1", "D1", 10.0]
    assert page["hexdump"].startswith("00000000  01 00 00 00 01 00 41 00")
    leaf = client.get("/api/tables/empleados/pages/1", params={"file": "idx"}).json()
    assert leaf["header"]["page_type_name"] == "BTREE_LEAF" and leaf["details"]["entries"][0]["key"] == 1
    assert client.get("/api/tables/empleados/pages/99").status_code == 400
    assert client.get("/api/tables/empleados/pages/1", params={"file": "zzz"}).status_code == 400
    assert client.get("/api/tables/nada/pages/1").status_code == 404


# ---------------------------------------------------------------------- Entrega 2
def test_consulta_multimodal_y_desglose_de_io(client):
    client.post("/api/query", json={"sql": (
        "CREATE TABLE locales (id INT PRIMARY KEY, nombre CHAR(20), ubicacion POINT, resena TEXT(80), v VECTOR(3));"
        "INSERT INTO locales VALUES (1, 'Centro', (-77.03, -12.05), 'cafe tranquilo con terraza', [1, 0, 0]),"
        " (2, 'Miraflores', (-77.03, -12.12), 'cafe con vista al mar', [0.9, 0.1, 0]),"
        " (3, 'Callao', (-77.12, -12.06), 'restaurante de pescado frente al mar', [0, 1, 0]);"
        "CREATE INDEX idx_geo ON locales (ubicacion) USING RTREE;"
        "CREATE INDEX idx_txt ON locales (resena) USING INVERTED;"
        "CREATE INDEX idx_v ON locales (v) USING IVF WITH (metric = 'COSINE');"
    )})
    body = client.post("/api/query", json={
        "sql": "SELECT id, nombre, ubicacion FROM locales WHERE ubicacion WITHIN_RADIUS((-77.03, -12.05), 8.5)"
    }).json()
    assert sorted(r[0] for r in body["rows"]) == [1, 2] and body["rows"][0][2] == [-77.03, -12.05]
    assert body["plan"]["type"] == "RTreeRadiusScan" and body["plan"]["spatial"]["kind"] == "radius"
    io = body["metrics"]["io"]
    assert io["index_reads"] >= 1 and io["table_reads"] == 2
    assert {f["role"] for f in io["files"]} == {"index", "table"}
    body = client.post("/api/query", json={
        "sql": "SELECT id, COSINE_SCORE() AS r FROM locales WHERE resena MATCH 'cafe AND mar' ORDER BY r DESC LIMIT 5"
    }).json()
    assert [r[0] for r in body["rows"]] == [2] and body["plan"]["text"]["mode"] == "boolean"
    body = client.post("/api/query", json={
        "sql": "SET ivf.probes = 3; SELECT id, v <=> '[1, 0, 0]' AS d FROM locales ORDER BY d LIMIT 2"
    }).json()
    assert [r[0] for r in body["rows"]] == [1, 2] and body["rows"][0][1] == 0.0
    assert [s["statement"] for s in body["statements"]] == ["SET", "SELECT"]
    tables = client.get("/api/tables").json()["tables"]
    kinds = {i["name"]: i["type"] for i in tables[0]["indexes"]}
    assert kinds == {"idx_geo": "RTREE", "idx_txt": "INVERTED", "idx_v": "IVF"}
    assert [c["type"] for c in tables[0]["columns"]] == ["INT", "CHAR", "POINT", "CHAR", "VECTOR"]
    files = {f["key"] for f in client.get("/api/tables/locales/files").json()["files"]}
    assert {"data", "idx_geo", "idx_txt:vocab.dic", "idx_txt:postings.inv", "idx_v:ivf.lst"} <= files
    page = client.get("/api/tables/locales/pages/1", params={"file": "idx_txt:vocab.dic"}).json()
    assert page["header"]["page_type_name"] == "INV_VOCAB"
    assert [e["term"] for e in page["details"]["entries"]][:2] == ["caf", "frent"]
    err = client.post("/api/query", json={"sql": "SELECT * FROM locales WHERE ubicacion WITHIN_RADIUS((0, 0), )"})
    assert err.status_code == 400 and err.json()["detail"]["error"] == "SyntaxError"


def test_aplicacion_sin_catalogo_cargado(client):
    status = client.get("/api/app/status").json()
    assert status == {**status, "table": "productos", "loaded": False, "info": None}
    assert set(status["methods"]) == {"embedding", "bovw", "exact", "text"}
    assert client.get("/api/app/samples").json() == {"products": []}
    res = client.post("/api/app/recommend", data={"text": "blue shirt"})
    assert res.status_code == 400 and "prepare_fashion" in res.json()["detail"]["message"]
    assert client.get("/api/media/123.jpg").status_code == 404
    assert client.get("/api/media/..%2Fcatalog.bin").status_code == 404
