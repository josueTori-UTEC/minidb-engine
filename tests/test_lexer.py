import pytest

from backend.sql.lexer import SQLSyntaxError, TokenType, tokenize


def test_tokens_basicos_y_posiciones():
    toks = tokenize("select *\n  FROM Empleados -- comentario\nWHERE id>=10;")
    assert [t.type for t in toks] == [
        TokenType.KEYWORD, TokenType.STAR, TokenType.KEYWORD, TokenType.IDENT, TokenType.KEYWORD,
        TokenType.IDENT, TokenType.OP, TokenType.INT, TokenType.SEMICOLON, TokenType.EOF,
    ]
    assert toks[0].value == "SELECT"  # palabras clave case-insensitive
    assert toks[3].value == "empleados"  # identificadores en minúsculas
    assert (toks[2].position.line, toks[2].position.column) == (2, 3)
    assert (toks[4].position.line, toks[4].position.column) == (3, 1)


def test_numeros_strings_y_operadores():
    toks = tokenize("3 4.5 .5 1e3 'O''Hara' 'ñandú' <> != <= >= < > =")
    assert [t.value for t in toks[:-1]] == [3, 4.5, 0.5, 1000.0, "O'Hara", "ñandú", "!=", "!=", "<=", ">=", "<", ">", "="]
    assert toks[3].type == TokenType.FLOAT


def test_comentario_de_bloque():
    assert [t.value for t in tokenize("SELECT /* nada \n aquí */ 1")[:-1]] == ["SELECT", 1]


@pytest.mark.parametrize(
    "sql, fragment",
    [("SELECT 'abc", "string sin cerrar"), ("SELECT @", "carácter inesperado"), ("SELECT 12abc", "número mal formado"),
     ("/* abierto", "comentario"), ("SELECT !", "carácter inesperado")],
)
def test_errores_lexicos(sql, fragment):
    with pytest.raises(SQLSyntaxError) as err:
        tokenize(sql)
    assert fragment in err.value.message
    assert err.value.position is not None
