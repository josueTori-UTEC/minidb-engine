"""Errores del motor SQL que se reportan al cliente."""

from backend.sql.lexer import SQLSyntaxError


class SemanticError(Exception):
    """La sentencia es sintácticamente válida pero no se puede ejecutar
    (tabla o columna inexistente, tipos incompatibles, PK duplicada, ...)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


__all__ = ["SQLSyntaxError", "SemanticError"]
