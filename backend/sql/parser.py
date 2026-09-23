"""Parser de descenso recursivo.

Gramática soportada (palabras clave case-insensitive, ``;`` opcional al final)::

    script       := statement { ';' statement } [';']
    statement    := create_table | create_index | insert | select | delete
                  | drop | copy | explain
    create_table := CREATE TABLE id '(' coldef {',' coldef} ')' [USING (HEAP|SEQUENTIAL)]
    coldef       := id type [PRIMARY KEY]
    type         := INT | INTEGER | FLOAT | CHAR '(' n ')' | VARCHAR '(' n ')'
    create_index := CREATE INDEX id ON id '(' id ')' [USING (BTREE|HASH)]
    insert       := INSERT INTO id VALUES tuple {',' tuple}
    tuple        := '(' literal {',' literal} ')'
    select       := SELECT ('*' | id {',' id}) FROM id [WHERE cond]
    delete       := DELETE FROM id [WHERE cond]
    cond         := pred {AND pred}
    pred         := id op literal | literal op id | id BETWEEN literal AND literal
    op           := '=' | '!=' | '<>' | '<' | '<=' | '>' | '>='
    drop         := DROP (TABLE | INDEX) id
    copy         := COPY id FROM string
    explain      := EXPLAIN (select | delete)
    literal      := ['+' | '-'] number | string

Si se omite ``USING`` la tabla es HEAP y el índice BTREE.
"""

from __future__ import annotations

from typing import Any, Iterator

from backend.sql import ast
from backend.sql.lexer import Lexer, SQLSyntaxError, Token, TokenType
from backend.storage.catalog import IndexKind, Organization
from backend.storage.record import ColumnType

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
            if tok.value == "EXPLAIN":
                self._advance()
                if self._is(TokenType.KEYWORD, "SELECT"):
                    return ast.Explain(self._select())
                if self._is(TokenType.KEYWORD, "DELETE"):
                    return ast.Explain(self._delete())
                raise self._error("SELECT o DELETE después de EXPLAIN")
        raise SQLSyntaxError(
            f"sentencia no reconocida: {tok.describe()} (se esperaba CREATE, INSERT, SELECT, DELETE, DROP, COPY o EXPLAIN)",
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
        else:
            raise self._error("un tipo (INT, FLOAT, CHAR(n) o VARCHAR(n))")
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
            method = IndexKind(self._keyword("BTREE", "HASH").value)
        return ast.CreateIndex(name, table, column, method)

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
        values = [self._literal()]
        while self._accept(TokenType.COMMA):
            values.append(self._literal())
        self._expect(TokenType.RPAREN, expected="',' o ')'")
        return values

    def _select(self) -> ast.Select:
        self._keyword("SELECT")
        columns: list[str] | None
        if self._accept(TokenType.STAR):
            columns = None
        else:
            columns = [self._identifier("una columna o '*'")]
            while self._accept(TokenType.COMMA):
                columns.append(self._identifier("nombre de columna"))
        self._keyword("FROM")
        table = self._identifier("nombre de tabla")
        return ast.Select(table, columns, self._where())

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
            if self._accept(TokenType.KEYWORD, "BETWEEN"):
                low = self._literal()
                self._keyword("AND")
                high = self._literal()
                return ast.Between(column, low, high)
            op = self._expect(TokenType.OP, expected="un operador (=, !=, <, <=, >, >=) o BETWEEN").value
            return ast.Comparison(column, op, self._literal())
        if self.current.type in (TokenType.INT, TokenType.FLOAT, TokenType.STRING, TokenType.MINUS, TokenType.PLUS):
            value = self._literal()
            op = self._expect(TokenType.OP, expected="un operador (=, !=, <, <=, >, >=)").value
            column = self._identifier("nombre de columna")
            return ast.Comparison(column, _FLIP[op], value)
        raise self._error("una condición (columna op valor)")

    def _copy(self) -> ast.Copy:
        self._keyword("COPY")
        table = self._identifier("nombre de tabla")
        self._keyword("FROM")
        path = self._expect(TokenType.STRING, expected="la ruta del CSV entre comillas simples").value
        return ast.Copy(table, path)


def parse(text: str) -> list[ast.Statement]:
    return Parser(text).parse()


def parse_one(text: str) -> ast.Statement:
    statements = parse(text)
    if len(statements) != 1:
        raise SQLSyntaxError(f"se esperaba una sentencia y se encontraron {len(statements)}")
    return statements[0]
