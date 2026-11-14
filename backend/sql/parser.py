"""Parser de descenso recursivo.

Gramática soportada (palabras clave case-insensitive, ``;`` opcional al final)::

    script       := statement { ';' statement } [';']
    statement    := create_table | create_index | insert | select | delete
                  | drop | copy | explain | set
    create_table := CREATE TABLE id '(' coldef {',' coldef} ')' [USING (HEAP|SEQUENTIAL)]
    coldef       := id type [PRIMARY KEY]
    type         := INT | INTEGER | FLOAT | CHAR '(' n ')' | VARCHAR '(' n ')'
                  | TEXT ['(' n ')'] | POINT | VECTOR '(' n ')'
    create_index := CREATE INDEX id ON id '(' id ')'
                    [USING (BTREE|HASH|RTREE|INVERTED|IVF)] [WITH '(' option {',' option} ')']
    option       := id '=' (string | number | id)
    insert       := INSERT INTO id VALUES tuple {',' tuple}
    tuple        := '(' value {',' value} ')'
    value        := literal | point | vector
    select       := SELECT ('*' | item {',' item}) FROM id [WHERE cond]
                    [ORDER BY expr [ASC|DESC]] [LIMIT n]
    item         := expr [AS id]
    expr         := id | COSINE_SCORE '(' ')' | id ('<->' | '<=>') (string | point | vector)
    delete       := DELETE FROM id [WHERE cond]
    cond         := pred {AND pred}
    pred         := id op literal | literal op id | id BETWEEN literal AND literal
                  | id MATCH string
                  | id WITHIN_RADIUS '(' point ',' number ')'
                  | id WITHIN_BOX '(' point ',' point ')'
    op           := '=' | '!=' | '<>' | '<' | '<=' | '>' | '>='
    drop         := DROP (TABLE | INDEX) id
    copy         := COPY id FROM string
    explain      := EXPLAIN (select | delete)
    set          := SET id ['.' id] '=' (literal | id)
    literal      := ['+' | '-'] number | string
    point        := [POINT] '(' number ',' number ')'
    vector       := '[' number {',' number} ']'

Si se omite ``USING`` la tabla es HEAP y el índice BTREE. ``TEXT``, ``POINT``,
``VECTOR`` y ``COSINE_SCORE`` no son palabras reservadas: se reconocen por
contexto, así que siguen siendo válidas como nombres de columna.
"""

from __future__ import annotations

from typing import Any, Iterator

from backend.sql import ast
from backend.sql.lexer import Lexer, SQLSyntaxError, Token, TokenType
from backend.storage.catalog import IndexKind, Organization
from backend.storage.record import ColumnType

DEFAULT_TEXT_SIZE = 1024

_FLIP = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self._tokens = Lexer(text).tokens()
        self.current: Token = next(self._tokens)
        self._last: Token = self.current

    # ------------------------------------------------------------------ utilidades
    def _advance(self) -> Token:
        token = self.current
        self._last = token
        if token.type != TokenType.EOF:
            self.current = next(self._tokens)
        return token

    def _is(self, ttype: TokenType, value: Any = None) -> bool:
        return self.current.type == ttype and (value is None or self.current.value == value)

    def _accept(self, ttype: TokenType, value: Any = None) -> Token | None:
        if self._is(ttype, value):
            return self._advance()
        return None

    def _error(self, expected: str) -> SQLSyntaxError:
        return SQLSyntaxError(f"se esperaba {expected} pero se encontró {self.current.describe()}", self.current.position)

    def _expect(self, ttype: TokenType, value: Any = None, expected: str | None = None) -> Token:
        if self._is(ttype, value):
            return self._advance()
        raise self._error(expected or (value if value is not None else ttype.value))

    def _keyword(self, *words: str) -> Token:
        for word in words:
            if self._is(TokenType.KEYWORD, word):
                return self._advance()
        raise self._error(" o ".join(words))

    def _identifier(self, what: str = "un identificador") -> str:
        if self.current.type == TokenType.IDENT:
            return self._advance().value
        if self.current.type == TokenType.KEYWORD:
            raise SQLSyntaxError(
                f"{self.current.describe()} es una palabra reservada y no puede usarse como {what}",
                self.current.position,
            )
        raise self._error(what)

    # ------------------------------------------------------------------ script
    def statements(self) -> Iterator[ast.Statement]:
        """Produce las sentencias una por una (el lexer avanza de forma perezosa)."""
        while True:
            while self._accept(TokenType.SEMICOLON):
                pass
            if self.current.type == TokenType.EOF:
                return
            start = self.current.position
            stmt = self._statement()
            end = self._last.position.offset + len(self._last.text)
            stmt.sql = self.text[start.offset : end]
            stmt.position = start
            if not self._is(TokenType.SEMICOLON) and not self._is(TokenType.EOF):
                raise self._error("';' o el fin de la consulta")
            yield stmt

    def parse(self) -> list[ast.Statement]:
        return list(self.statements())

    def _statement(self) -> ast.Statement:
        tok = self.current
        if tok.type == TokenType.KEYWORD:
            if tok.value == "CREATE":
                self._advance()
                if self._accept(TokenType.KEYWORD, "TABLE"):
                    return self._create_table()
                if self._accept(TokenType.KEYWORD, "INDEX"):
                    return self._create_index()
                raise self._error("TABLE o INDEX")
            if tok.value == "INSERT":
                return self._insert()
            if tok.value == "SELECT":
                return self._select()
            if tok.value == "DELETE":
                return self._delete()
            if tok.value == "DROP":
                self._advance()
                if self._accept(TokenType.KEYWORD, "TABLE"):
                    return ast.DropTable(self._identifier("nombre de tabla"))
                if self._accept(TokenType.KEYWORD, "INDEX"):
                    return ast.DropIndex(self._identifier("nombre de índice"))
                raise self._error("TABLE o INDEX")
            if tok.value == "COPY":
                return self._copy()
            if tok.value == "SET":
                return self._set()
            if tok.value == "EXPLAIN":
                self._advance()
                if self._is(TokenType.KEYWORD, "SELECT"):
                    return ast.Explain(self._select())
                if self._is(TokenType.KEYWORD, "DELETE"):
                    return ast.Explain(self._delete())
                raise self._error("SELECT o DELETE después de EXPLAIN")
        raise SQLSyntaxError(
            f"sentencia no reconocida: {tok.describe()} (se esperaba CREATE, INSERT, SELECT, DELETE, DROP, COPY, SET o EXPLAIN)",
            tok.position,
        )

    # ------------------------------------------------------------------ DDL
    def _create_table(self) -> ast.CreateTable:
        name = self._identifier("nombre de tabla")
        self._expect(TokenType.LPAREN)
        columns = [self._column_def()]
        while self._accept(TokenType.COMMA):
            columns.append(self._column_def())
        self._expect(TokenType.RPAREN, expected="',' o ')'")
        organization = Organization.HEAP
        if self._accept(TokenType.KEYWORD, "USING"):
            organization = Organization(self._keyword("HEAP", "SEQUENTIAL").value)
        return ast.CreateTable(name, columns, organization)

    def _column_def(self) -> ast.ColumnDef:
        name = self._identifier("nombre de columna")
        type_tok = self.current
        if self._accept(TokenType.KEYWORD, "INT") or self._accept(TokenType.KEYWORD, "INTEGER"):
            col = ast.ColumnDef(name, ColumnType.INT)
        elif self._accept(TokenType.KEYWORD, "FLOAT"):
            col = ast.ColumnDef(name, ColumnType.FLOAT)
        elif self._accept(TokenType.KEYWORD, "CHAR") or self._accept(TokenType.KEYWORD, "VARCHAR"):
            self._expect(TokenType.LPAREN, expected=f"'(' después de {type_tok.value}")
            size_tok = self._expect(TokenType.INT, expected="el tamaño (entero)")
            self._expect(TokenType.RPAREN)
            col = ast.ColumnDef(name, ColumnType.CHAR, size_tok.value)
        elif self._accept(TokenType.IDENT, "point"):
            col = ast.ColumnDef(name, ColumnType.POINT)
        elif self._accept(TokenType.IDENT, "vector"):
            self._expect(TokenType.LPAREN, expected="'(' después de VECTOR")
            size_tok = self._expect(TokenType.INT, expected="la dimensión del vector (entero)")
            self._expect(TokenType.RPAREN)
            col = ast.ColumnDef(name, ColumnType.VECTOR, size_tok.value)
        elif self._accept(TokenType.IDENT, "text"):
            size = DEFAULT_TEXT_SIZE
            if self._accept(TokenType.LPAREN):
                size = self._expect(TokenType.INT, expected="el tamaño (entero)").value
                self._expect(TokenType.RPAREN)
            col = ast.ColumnDef(name, ColumnType.CHAR, size)
        else:
            raise self._error("un tipo (INT, FLOAT, CHAR(n), VARCHAR(n), TEXT(n), POINT o VECTOR(n))")
        if self._accept(TokenType.KEYWORD, "PRIMARY"):
            self._keyword("KEY")
            col.primary_key = True
        return col

    def _create_index(self) -> ast.CreateIndex:
        name = self._identifier("nombre de índice")
        self._keyword("ON")
        table = self._identifier("nombre de tabla")
        self._expect(TokenType.LPAREN)
        column = self._identifier("nombre de columna")
        self._expect(TokenType.RPAREN)
        method = IndexKind.BTREE
        if self._accept(TokenType.KEYWORD, "USING"):
            method = IndexKind(self._keyword("BTREE", "HASH", "RTREE", "INVERTED", "IVF").value)
        options: dict[str, str] = {}
        if self._accept(TokenType.KEYWORD, "WITH"):
            self._expect(TokenType.LPAREN, expected="'(' después de WITH")
            while True:
                key = self._identifier("el nombre de una opción")
                self._expect(TokenType.OP, "=", expected="'='")
                tok = self.current
                if tok.type in (TokenType.STRING, TokenType.INT, TokenType.FLOAT, TokenType.IDENT, TokenType.KEYWORD):
                    self._advance()
                    options[key] = str(tok.value)
                else:
                    raise self._error("el valor de la opción")
                if not self._accept(TokenType.COMMA):
                    break
            self._expect(TokenType.RPAREN, expected="',' o ')'")
        return ast.CreateIndex(name, table, column, method, options)

    # ------------------------------------------------------------------ DML
    def _literal(self) -> Any:
        sign = 1
        if self._accept(TokenType.MINUS):
            sign = -1
        elif self._accept(TokenType.PLUS):
            pass
        else:
            tok = self._accept(TokenType.STRING)
            if tok is not None:
                return tok.value
        tok = self.current
        if tok.type in (TokenType.INT, TokenType.FLOAT):
            self._advance()
            return sign * tok.value
        raise self._error("un literal (número o 'texto')")

    def _insert(self) -> ast.Insert:
        self._keyword("INSERT")
        self._keyword("INTO")
        table = self._identifier("nombre de tabla")
        self._keyword("VALUES")
        rows = [self._tuple()]
        while self._accept(TokenType.COMMA):
            rows.append(self._tuple())
        return ast.Insert(table, rows)

    def _tuple(self) -> list[Any]:
        self._expect(TokenType.LPAREN)
        values = [self._value()]
        while self._accept(TokenType.COMMA):
            values.append(self._value())
        self._expect(TokenType.RPAREN, expected="',' o ')'")
        return values

    def _number(self) -> float:
        sign = -1 if self._accept(TokenType.MINUS) else 1
        if sign == 1:
            self._accept(TokenType.PLUS)
        tok = self.current
        if tok.type in (TokenType.INT, TokenType.FLOAT):
            self._advance()
            return sign * tok.value
        raise self._error("un número")

    def _is_point_start(self) -> bool:
        return self._is(TokenType.LPAREN) or self._is(TokenType.IDENT, "point")

    def _point(self) -> ast.Point:
        """``(x, y)`` o ``POINT(x, y)``."""
        self._accept(TokenType.IDENT, "point")
        self._expect(TokenType.LPAREN, expected="un punto '(x, y)'")
        x = self._number()
        self._expect(TokenType.COMMA, expected="','")
        y = self._number()
        self._expect(TokenType.RPAREN)
        return float(x), float(y)

    def _vector(self) -> tuple[float, ...]:
        """``[v1, v2, ...]``."""
        self._expect(TokenType.LBRACKET)
        values = [float(self._number())]
        while self._accept(TokenType.COMMA):
            values.append(float(self._number()))
        self._expect(TokenType.RBRACKET, expected="',' o ']'")
        return tuple(values)

    def _value(self) -> Any:
        """Valor de un INSERT: literal, punto o vector."""
        if self._is_point_start():
            return self._point()
        if self._is(TokenType.LBRACKET):
            return self._vector()
        return self._literal()

    def _operand(self) -> Any:
        """Operando de una distancia: ``'[...]'`` (string), punto o vector."""
        if self._is_point_start():
            return self._point()
        if self._is(TokenType.LBRACKET):
            return self._vector()
        return self._expect(TokenType.STRING, expected="un vector '[...]' o un punto (x, y)").value

    def _expr(self, what: str) -> ast.Expr:
        name = self._identifier(what)
        if name == "cosine_score" and self._accept(TokenType.LPAREN):
            self._expect(TokenType.RPAREN, expected="')'")
            return ast.Expr("score")
        if self._is(TokenType.OP, "<->") or self._is(TokenType.OP, "<=>"):
            op = self._advance().value
            return ast.Expr("distance", name, op, self._operand())
        return ast.Expr("column", name)

    def _select(self) -> ast.Select:
        self._keyword("SELECT")
        columns: list[str] | None
        items: list[ast.SelectItem] | None = None
        if self._accept(TokenType.STAR):
            columns = None
        else:
            items = [self._select_item("una columna o '*'")]
            while self._accept(TokenType.COMMA):
                items.append(self._select_item("nombre de columna"))
            if all(i.expr.kind == "column" and i.alias is None for i in items):
                columns = [str(i.expr.column) for i in items]
                items = None
            else:
                columns = None
        self._keyword("FROM")
        table = self._identifier("nombre de tabla")
        where = self._where()
        order_by = None
        if self._accept(TokenType.KEYWORD, "ORDER"):
            self._keyword("BY")
            expr = self._expr("una columna, un alias o una expresión de distancia")
            descending = False
            if self._accept(TokenType.KEYWORD, "DESC"):
                descending = True
            else:
                self._accept(TokenType.KEYWORD, "ASC")
            order_by = ast.OrderBy(expr, descending)
        limit = None
        if self._accept(TokenType.KEYWORD, "LIMIT"):
            limit = self._expect(TokenType.INT, expected="el número de filas (entero)").value
        return ast.Select(table, columns, where, items, order_by, limit)

    def _select_item(self, what: str) -> ast.SelectItem:
        expr = self._expr(what)
        alias = self._identifier("un alias") if self._accept(TokenType.KEYWORD, "AS") else None
        return ast.SelectItem(expr, alias)

    def _delete(self) -> ast.Delete:
        self._keyword("DELETE")
        self._keyword("FROM")
        table = self._identifier("nombre de tabla")
        return ast.Delete(table, self._where())

    def _where(self) -> list[ast.Predicate]:
        if not self._accept(TokenType.KEYWORD, "WHERE"):
            return []
        preds = [self._predicate()]
        while self._accept(TokenType.KEYWORD, "AND"):
            preds.append(self._predicate())
        return preds

    def _predicate(self) -> ast.Predicate:
        if self.current.type == TokenType.IDENT:
            column = self._advance().value
            if self._accept(TokenType.KEYWORD, "MATCH"):
                query = self._expect(TokenType.STRING, expected="la consulta de texto entre comillas simples").value
                return ast.Match(column, query)
            if self._accept(TokenType.KEYWORD, "WITHIN_RADIUS"):
                self._expect(TokenType.LPAREN, expected="'(' después de WITHIN_RADIUS")
                center = self._point()
                self._expect(TokenType.COMMA, expected="',' y el radio en km")
                radius = self._number()
                self._expect(TokenType.RPAREN)
                if radius < 0:
                    raise SQLSyntaxError("el radio no puede ser negativo", self._last.position)
                return ast.WithinRadius(column, center, float(radius))
            if self._accept(TokenType.KEYWORD, "WITHIN_BOX"):
                self._expect(TokenType.LPAREN, expected="'(' después de WITHIN_BOX")
                a = self._point()
                self._expect(TokenType.COMMA, expected="',' y la otra esquina")
                b = self._point()
                self._expect(TokenType.RPAREN)
                low = (min(a[0], b[0]), min(a[1], b[1]))
                high = (max(a[0], b[0]), max(a[1], b[1]))
                return ast.WithinBox(column, low, high)
            if self._accept(TokenType.KEYWORD, "BETWEEN"):
                low = self._literal()
                self._keyword("AND")
                high = self._literal()
                return ast.Between(column, low, high)
            op_tok = self._expect(
                TokenType.OP, expected="un operador (=, !=, <, <=, >, >=), BETWEEN, MATCH o WITHIN_RADIUS"
            )
            if op_tok.value not in _FLIP:
                raise SQLSyntaxError(
                    f"'{op_tok.value}' es un operador de distancia: úselo en la lista de selección o en ORDER BY",
                    op_tok.position,
                )
            return ast.Comparison(column, op_tok.value, self._literal())
        if self.current.type in (TokenType.INT, TokenType.FLOAT, TokenType.STRING, TokenType.MINUS, TokenType.PLUS):
            value = self._literal()
            op_tok = self._expect(TokenType.OP, expected="un operador (=, !=, <, <=, >, >=)")
            if op_tok.value not in _FLIP:
                raise SQLSyntaxError(f"operador '{op_tok.value}' no válido en una comparación", op_tok.position)
            op = op_tok.value
            column = self._identifier("nombre de columna")
            return ast.Comparison(column, _FLIP[op], value)
        raise self._error("una condición (columna op valor)")

    def _copy(self) -> ast.Copy:
        self._keyword("COPY")
        table = self._identifier("nombre de tabla")
        self._keyword("FROM")
        path = self._expect(TokenType.STRING, expected="la ruta del CSV entre comillas simples").value
        return ast.Copy(table, path)


    def _set(self) -> ast.Set:
        self._keyword("SET")
        first = self.current
        if first.type not in (TokenType.IDENT, TokenType.KEYWORD):
            raise self._error("el nombre del parámetro")
        self._advance()
        name = str(first.value).lower()
        if self._accept(TokenType.DOT):
            # el segundo componente puede coincidir con una palabra reservada (p. ej. ivf.lists)
            tok = self.current
            if tok.type not in (TokenType.IDENT, TokenType.KEYWORD):
                raise self._error("el nombre del parámetro")
            self._advance()
            name = f"{name}.{str(tok.value).lower()}"
        self._expect(TokenType.OP, "=", expected="'='")
        tok = self.current
        if tok.type in (TokenType.IDENT, TokenType.KEYWORD):
            self._advance()
            return ast.Set(name, str(tok.value).lower())
        return ast.Set(name, self._literal())


def parse(text: str) -> list[ast.Statement]:
    return Parser(text).parse()


def parse_one(text: str) -> ast.Statement:
    statements = parse(text)
    if len(statements) != 1:
        raise SQLSyntaxError(f"se esperaba una sentencia y se encontraron {len(statements)}")
    return statements[0]
