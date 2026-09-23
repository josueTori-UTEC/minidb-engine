"""Analizador léxico de SQL.

Produce tokens de forma perezosa (el parser los pide a medida que avanza), con
su posición (línea, columna y offset) para reportar errores. Palabras clave e
identificadores son case-insensitive (los identificadores se normalizan a
minúsculas); los strings van entre comillas simples y ``''`` escapa una comilla.
Se ignoran comentarios ``-- ...`` y ``/* ... */``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator

from backend.sql.ast import Position

KEYWORDS = frozenset(
    """
    CREATE TABLE INSERT INTO VALUES SELECT FROM WHERE AND DELETE INDEX ON USING
    PRIMARY KEY HEAP SEQUENTIAL BTREE HASH INT INTEGER FLOAT CHAR VARCHAR BETWEEN
    DROP COPY EXPLAIN
    """.split()
)


class TokenType(str, Enum):
    KEYWORD = "palabra clave"
    IDENT = "identificador"
    INT = "entero"
    FLOAT = "número"
    STRING = "string"
    OP = "operador"
    LPAREN = "'('"
    RPAREN = "')'"
    COMMA = "','"
    SEMICOLON = "';'"
    STAR = "'*'"
    MINUS = "'-'"
    PLUS = "'+'"
    EOF = "fin de la consulta"


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: Any
    text: str
    position: Position

    def describe(self) -> str:
        if self.type == TokenType.EOF:
            return "el fin de la consulta"
        return f"'{self.text}'"


class SQLSyntaxError(Exception):
    def __init__(self, message: str, position: Position | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.position = position

    def __str__(self) -> str:
        if self.position is None:
            return self.message
        return f"{self.message} (línea {self.position.line}, columna {self.position.column})"


_SINGLE = {"(": TokenType.LPAREN, ")": TokenType.RPAREN, ",": TokenType.COMMA, ";": TokenType.SEMICOLON,
           "*": TokenType.STAR, "-": TokenType.MINUS, "+": TokenType.PLUS}


class Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        # Seguimiento incremental de línea/columna (los offsets pedidos son crecientes).
        self._tracked = 0
        self._line = 1
        self._line_start = 0

    def _position(self, offset: int | None = None) -> Position:
        offset = self.pos if offset is None else offset
        if offset < self._tracked:
            line = self.text.count("\n", 0, offset) + 1
            return Position(line, offset - (self.text.rfind("\n", 0, offset) + 1) + 1, offset)
        newlines = self.text.count("\n", self._tracked, offset)
        if newlines:
            self._line += newlines
            self._line_start = self.text.rfind("\n", self._tracked, offset) + 1
        self._tracked = offset
        return Position(self._line, offset - self._line_start + 1, offset)

    def _skip_space_and_comments(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch.isspace():
                self.pos += 1
            elif text.startswith("--", self.pos):
                end = text.find("\n", self.pos)
                self.pos = n if end < 0 else end + 1
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    raise SQLSyntaxError("comentario /* sin cerrar", self._position())
                self.pos = end + 2
            else:
                return

    def tokens(self) -> Iterator[Token]:
        text, n = self.text, len(self.text)
        while True:
            self._skip_space_and_comments()
            if self.pos >= n:
                yield Token(TokenType.EOF, None, "", self._position())
                return
            start = self.pos
            ch = text[start]
            if ch.isalpha() or ch == "_":
                end = start + 1
                while end < n and (text[end].isalnum() or text[end] == "_"):
                    end += 1
                word = text[start:end]
                self.pos = end
                upper = word.upper()
                if upper in KEYWORDS:
                    yield Token(TokenType.KEYWORD, upper, word, self._position(start))
                else:
                    yield Token(TokenType.IDENT, word.lower(), word, self._position(start))
            elif ch.isdigit() or (ch == "." and start + 1 < n and text[start + 1].isdigit()):
                yield self._number(start)
            elif ch == "'":
                yield self._string(start)
            elif ch in "<>!=":
                two = text[start : start + 2]
                if two in ("<=", ">=", "!=", "<>"):
                    self.pos += 2
                    yield Token(TokenType.OP, "!=" if two == "<>" else two, two, self._position(start))
                elif ch in "<>=":
                    self.pos += 1
                    yield Token(TokenType.OP, ch, ch, self._position(start))
                else:
                    raise SQLSyntaxError(f"carácter inesperado '{ch}'", self._position(start))
            elif ch in _SINGLE:
                self.pos += 1
                yield Token(_SINGLE[ch], ch, ch, self._position(start))
            else:
                raise SQLSyntaxError(f"carácter inesperado '{ch}'", self._position(start))

    def _number(self, start: int) -> Token:
        text, n = self.text, len(self.text)
        end = start
        while end < n and text[end].isdigit():
            end += 1
        is_float = False
        if end < n and text[end] == ".":
            is_float = True
            end += 1
            while end < n and text[end].isdigit():
                end += 1
        if end < n and text[end] in "eE":
            exp_end = end + 1
            if exp_end < n and text[exp_end] in "+-":
                exp_end += 1
            if exp_end < n and text[exp_end].isdigit():
                is_float = True
                end = exp_end
                while end < n and text[end].isdigit():
                    end += 1
        if end < n and (text[end].isalpha() or text[end] == "_"):
            raise SQLSyntaxError(f"número mal formado '{text[start:end + 1]}'", self._position(start))
        raw = text[start:end]
        self.pos = end
        if is_float:
            return Token(TokenType.FLOAT, float(raw), raw, self._position(start))
        return Token(TokenType.INT, int(raw), raw, self._position(start))

    def _string(self, start: int) -> Token:
        text, n = self.text, len(self.text)
        parts = []
        i = start + 1
        while True:
            end = text.find("'", i)
            if end < 0:
                raise SQLSyntaxError("string sin cerrar", self._position(start))
            parts.append(text[i:end])
            if end + 1 < n and text[end + 1] == "'":
                parts.append("'")
                i = end + 2
                continue
            self.pos = end + 1
            return Token(TokenType.STRING, "".join(parts), text[start : end + 1], self._position(start))


def tokenize(text: str) -> list[Token]:
    """Lista completa de tokens (útil en tests)."""
    return list(Lexer(text).tokens())
