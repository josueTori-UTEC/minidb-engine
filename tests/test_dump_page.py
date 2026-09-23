from backend.database import Database
from backend.tools.dump_page import hexdump, inspect_page, main, page_summary, schema_from_catalog


def test_hexdump_formato_y_lineas_repetidas():
    out = hexdump(b"MDB1" + bytes(60))
    lines = out.splitlines()
    assert lines[0].startswith("00000000  4d 44 42 31 00") and lines[0].endswith("|MDB1............|")
    assert lines[2] == "*" and lines[-1] == "00000040"


def test_inspect_todos_los_tipos(tmp_path, capsys):
    db = Database(tmp_path, page_size=1024)
    db.execute("CREATE TABLE s (id INT PRIMARY KEY, n CHAR(10)) USING SEQUENTIAL")
    db.execute("INSERT INTO s VALUES " + ", ".join(f"({i}, 'n{i}')" for i in range(300, 0, -1)))
    db.execute("CREATE INDEX b ON s (id)")
    db.execute("CREATE INDEX h ON s (n) USING HASH")
    db.execute("DELETE FROM s WHERE id = 5")
    db.close()
    seq = inspect_page(tmp_path / "s.seq", 2, schema=schema_from_catalog(tmp_path / "s.seq"))
    assert seq["header"]["page_type_name"] == "SEQ_MAIN"
    first = seq["details"]["records"][0]["values"]
    assert isinstance(seq["details"]["fence_key"], int) and first[1] == f"n{first[0]}"
    assert "records" in inspect_page(tmp_path / "s.seq", 1)["details"]
    meta = inspect_page(tmp_path / "s_n.hsh", 0)
    assert meta["file_kind"] == "HASH" and meta["details"]["fields"]["segments"]
    assert inspect_page(tmp_path / "s_n.hsh", 1)["details"]["kind"] == "directorio"
    root = inspect_page(tmp_path / "s_id.bpt", 0)["details"]["fields"]["root_page_id"]
    node = inspect_page(tmp_path / "s_id.bpt", root)
    assert node["details"]["kind"] in ("interno", "hoja")
    assert inspect_page(tmp_path / "catalog.bin", 1)["header"]["page_type_name"] == "CATALOG"
    assert len(page_summary(tmp_path / "s.seq")) >= 2
    assert main([str(tmp_path / "s.seq"), "1"]) == 0
    out = capsys.readouterr().out
    assert "Header (24 bytes" in out and "Hexdump:" in out
    assert main([str(tmp_path / "s.ovf"), "--all"]) == 0
