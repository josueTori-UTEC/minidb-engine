"""Preprocesamiento léxico: del texto libre a una bolsa de palabras (BoW).

Pipeline (``spanish`` / ``english``):

1. normalización: minúsculas (*case folding*) y eliminación de tildes y diacríticos;
2. tokenización: secuencias de letras y dígitos;
3. eliminación de *stopwords* (``stoplist_es.txt`` / ``stoplist_en.txt``);
4. *stemming* con el algoritmo Snowball de ``nltk``.

El analizador ``raw`` no normaliza: separa por espacios y admite ``termino:n``
para indicar la frecuencia. Lo usa el índice invertido sobre palabras visuales
(BoVW), donde cada "documento" es el histograma de una imagen.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path

ANALYZERS = ("spanish", "english", "raw")
_TOKEN = re.compile(r"[a-z0-9]+")
_HERE = Path(__file__).resolve().parent
_STOPLISTS = {"spanish": "stoplist_es.txt", "english": "stoplist_en.txt"}


def normalize(text: str) -> str:
    """Minúsculas sin tildes ni diacríticos (``Camión`` -> ``camion``)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@lru_cache(maxsize=None)
def stopwords(language: str) -> frozenset[str]:
    path = _HERE / _STOPLISTS[language]
    return frozenset(path.read_text(encoding="utf-8").split())


@lru_cache(maxsize=None)
def _stemmer(language: str):
    from nltk.stem.snowball import SnowballStemmer

    return SnowballStemmer(language)


@lru_cache(maxsize=200_000)
def _stem(language: str, token: str) -> str:
    return _stemmer(language).stem(token)


class Analyzer:
    def __init__(self, name: str = "spanish") -> None:
        if name not in ANALYZERS:
            raise ValueError(f"analizador desconocido '{name}' (use {', '.join(ANALYZERS)})")
        self.name = name
        self._stop = frozenset() if name == "raw" else stopwords(name)

    def tokens(self, text: str) -> list[str]:
        """Términos normalizados en orden de aparición (sin *stopwords*)."""
        if self.name == "raw":
            out = []
            for part in text.split():
                term, _, count = part.partition(":")
                if term:
                    out.extend([term] * (int(count) if count.isdigit() else 1))
            return out
        stop = self._stop
        return [_stem(self.name, tok) for tok in _TOKEN.findall(normalize(text)) if tok not in stop]

    def term_frequencies(self, text: str) -> Counter[str]:
        """Bolsa de palabras: término -> frecuencia en el documento."""
        if self.name == "raw":
            tf: Counter[str] = Counter()
            for part in text.split():
                term, _, count = part.partition(":")
                if term:
                    tf[term] += int(count) if count.isdigit() else 1
            return tf
        return Counter(self.tokens(text))

    def term(self, word: str) -> str | None:
        """Normaliza una palabra de la consulta; ``None`` si es una *stopword*."""
        toks = self.tokens(word)
        return toks[0] if toks else None
