import pytest

from backend.storage.record import Column, ColumnType, KeyCodec, Schema, SchemaError, encode_str


def test_empleados_mide_62_bytes(empleados_schema):
    assert empleados_schema.struct.format == "<i30s20sd"
    assert empleados_schema.record_size == 62
    assert empleados_schema.offsets == (0, 4, 34, 54)
    assert empleados_schema.pk_index == 0


def test_round_trip(empleados_schema):
    values = empleados_schema.coerce((101, "Ada Lovelace", "Analytics", 5200))
    raw = empleados_schema.encode(values)
    assert len(raw) == 62
    assert empleados_schema.decode(raw) == (101, "Ada Lovelace", "Analytics", 5200.0)
    assert empleados_schema.decode(b"\xff" * 5 + raw, 5) == values


def test_truncado_por_bytes_sin_cortar_multibyte():
    col = Column("n", ColumnType.CHAR, 5)
    assert col.coerce("abcdefgh") == "abcde"
    # 'ñ' ocupa 2 bytes: "aññ" = 5 bytes cabe; "ññña" = 6 bytes -> "ññ"
    assert col.coerce("aññ") == "aññ"
    assert col.coerce("ññña") == "ññ"
    assert encode_str("€€", 4) == "€".encode()  # € = 3 bytes


def test_coercion_y_errores():
    i = Column("i", ColumnType.INT)
    f = Column("f", ColumnType.FLOAT)
    assert i.coerce(5.0) == 5
    assert f.coerce(3) == 3.0 and isinstance(f.coerce(3), float)
    assert f.coerce(-0.0) == 0.0
    for bad in (5.5, "5", True, 2**31):
        with pytest.raises(SchemaError):
            i.coerce(bad)
    with pytest.raises(SchemaError):
        f.coerce("x")
    with pytest.raises(SchemaError):
        Column("c", ColumnType.CHAR, 0)
    with pytest.raises(SchemaError):
        Column("i", ColumnType.INT, 4)


def test_schema_invalido():
    with pytest.raises(SchemaError):
        Schema([Column("a", ColumnType.INT), Column("a", ColumnType.INT)])
    with pytest.raises(SchemaError):
        Schema([Column("a", ColumnType.INT, primary_key=True), Column("b", ColumnType.INT, primary_key=True)])
    with pytest.raises(SchemaError):
        Schema([Column("a", ColumnType.INT)]).coerce((1, 2))


def test_key_codec():
    k = KeyCodec(Column("dept", ColumnType.CHAR, 20))
    raw = k.encode("Ventas")
    assert len(raw) == 20 and k.decode(raw) == "Ventas"
    ki = KeyCodec(Column("id", ColumnType.INT))
    assert ki.size == 4 and ki.decode(ki.encode(-7)) == -7
    assert ki.normalize(7.0) == 7
