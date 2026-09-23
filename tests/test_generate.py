import csv
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("generate", Path(__file__).resolve().parent.parent / "data" / "generate.py")
generate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate)


def test_ids_unicos_en_orden_aleatorio_y_determinista(tmp_path):
    rows = list(generate.generate_rows(5000, seed=7))
    ids = [r[0] for r in rows]
    assert sorted(ids) == list(range(1, 5001)) and ids != sorted(ids)
    assert rows == list(generate.generate_rows(5000, seed=7))
    assert rows != list(generate.generate_rows(5000, seed=8))
    for _, nombre, dept, salario in rows:
        assert len(nombre.encode()) <= 30 and len(dept.encode()) <= 20
        assert 1025.0 <= salario <= 15000.0


def test_csv_con_encabezado(tmp_path):
    path = generate.write_csv(tmp_path / "e.csv", 100, seed=1)
    with open(path, newline="", encoding="utf-8") as fh:
        data = list(csv.reader(fh))
    assert data[0] == ["id", "nombre", "dept", "salario"] and len(data) == 101
    assert generate.main(["--n", "10", "--out", str(tmp_path / "x.csv"), "--sorted"]) == 0
    with open(tmp_path / "x.csv", newline="", encoding="utf-8") as fh:
        assert [int(r[0]) for r in list(csv.reader(fh))[1:]] == list(range(1, 11))
