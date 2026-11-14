"""Índice invertido persistido en disco (recuperación booleana y ranking TF-IDF / BM25).

Tres archivos binarios paginados por índice (``<tabla>_<col>`` = ``base``):

``base.vocab.dic`` — **diccionario**
    Página 0: metadatos (nº de términos, ``N`` documentos, total de posteos,
    longitud media, analizador, esquema de puntuación, bandera ``stale``).
    Páginas 1..V (``INV_VOCAB``): entradas de tamaño fijo **ordenadas por
    término**: ``<término (40 B), df (u32), longitud de la lista (u32), offset (u64)>``
    = 56 bytes, 72 por página de 4 KB. ``offset`` es la posición (en posteos) de
    la lista dentro de ``postings.inv``. Como las entradas están ordenadas, un
    término se localiza con **búsqueda binaria sobre páginas**: ``⌈log2 V⌉``
    lecturas, sin cargar el diccionario en RAM.

``base.postings.inv`` — **listas de posteo**
    Páginas 1..P (``INV_POSTINGS``): la concatenación de todas las listas, cada
    una ordenada ascendentemente por ``doc_id``; un posteo es
    ``<doc_id (u32), tf (u16)>`` = 6 bytes, 678 por página. Para un término se
    leen únicamente las páginas ``offset // 678 .. (offset + df - 1) // 678``.

``base.docs.bin`` — **documentos**
    Dos regiones de entradas de 8 bytes (509 por página, direccionadas por
    ``doc_id``): primero las estadísticas ``<norma L2 (f32), longitud (u32)>`` y
    luego el ``RID`` del registro en la tabla. Así el ranking solo lee las
    páginas de normas de los candidatos y los RID únicamente del Top-K.

**Construcción (SPIMI).** Se recorre la tabla una sola vez; los posteos se
acumulan en un diccionario en memoria y, cada ``block_postings`` posteos, el
bloque se escribe ordenado por término en un archivo temporal de *runs*. Al
final se hace una mezcla de ``k`` vías de los runs (los ``doc_id`` crecen de un
bloque al siguiente, así que concatenar las listas conserva el orden) que escribe
``postings.inv`` y ``vocab.dic`` secuencialmente y acumula la norma de cada
documento. Nunca hay más de un bloque de posteos en memoria.

**Consultas.**

* Booleanas (``a AND b``, ``a OR b``, ``a AND NOT b``, paréntesis): mezcla con
  dos punteros sobre las listas ordenadas, en tiempo ``O(n + m)``.
* Ranking: ``w(t,d) = (1 + log10 tf) · log10(N / df)`` y similitud de coseno
  (``CosineScore``): se acumulan los productos solo sobre las listas de los
  términos de la consulta, se divide por ``‖d‖·‖q‖`` y se devuelve el Top-K con
  un *heap*. Alternativamente BM25 (``k1 = 1.2``, ``b = 0.75``).

El índice es estático: si la tabla cambia después de construirlo se marca como
desactualizado (``stale``) y se reconstruye antes de la siguiente búsqueda.
"""

from __future__ import annotations

import heapq
import math
import os
import re
import struct
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np

from backend.storage.disk_manager import DiskCounter, DiskManager
from backend.storage.page import (
    PAGE_HEADER_SIZE,
    FileKind,
    MetaCodec,
    PageHeader,
    PageType,
    expect_page_type,
)
from backend.storage.rid import RID
from backend.text.analyzer import ANALYZERS, Analyzer

TERM_SIZE = 40
SCORINGS = ("TFIDF_COSINE", "BM25")
BM25_K1 = 1.2
BM25_B = 0.75
DEFAULT_BLOCK_POSTINGS = 1_000_000

VOCAB_ENTRY = np.dtype([("term", f"S{TERM_SIZE}"), ("df", "<u4"), ("length", "<u4"), ("offset", "<u8")])
POSTING = np.dtype([("doc", "<u4"), ("tf", "<u2")])
DOC_STAT = np.dtype([("norm", "<f4"), ("length", "<u4")])
DOC_RID = np.dtype([("page", "<i4"), ("slot", "<u2"), ("pad", "<u2")])
assert VOCAB_ENTRY.itemsize == 56 and POSTING.itemsize == 6 and DOC_STAT.itemsize == 8 and DOC_RID.itemsize == 8

VOCAB_META = MetaCodec(
    FileKind.INV_VOCAB,
    [
        ("num_terms", "I"),
        ("num_docs", "I"),
        ("total_postings", "Q"),
        ("avg_length", "d"),
        ("analyzer", "B"),
        ("scoring", "B"),
        ("stale", "B"),
        ("built", "B"),
    ],
)
POSTINGS_META = MetaCodec(FileKind.INV_POSTINGS, [("total_postings", "Q")])
DOCS_META = MetaCodec(FileKind.INV_DOCS, [("num_docs", "I"), ("stat_pages", "I")])

_RUN_HEAD = struct.Struct("<BI")  # longitud del término, nº de posteos
_QUERY_TOKEN = re.compile(r"\(|\)|[^\s()]+")


def _truncate(term: str) -> str:
    """Recorta un término a ``TERM_SIZE`` bytes UTF-8 (lo que cabe en el diccionario)."""
    return term.encode("utf-8")[:TERM_SIZE].decode("utf-8", errors="ignore")


class InvertedIndexError(Exception):
    """Consulta mal formada o índice en un estado inválido."""


# ---------------------------------------------------------------------- flujo de bytes paginado
class _PageStreamWriter:
    """Escribe un flujo de bytes en páginas consecutivas (header + trozo del flujo)."""

    def __init__(self, dm: DiskManager, page_type: PageType) -> None:
        self.dm = dm
        self.page_type = page_type
        self.payload = dm.page_size - PAGE_HEADER_SIZE
        self._buf = bytearray()
        self.first_page = dm.num_pages()

    def write(self, data: bytes) -> None:
        self._buf += data
        while len(self._buf) >= self.payload:
            self._emit(bytes(self._buf[: self.payload]))
            del self._buf[: self.payload]

    def _emit(self, chunk: bytes) -> None:
        page_id = self.dm.allocate_page()
        buf = bytearray(self.dm.page_size)
        PageHeader(page_id=page_id, page_type=self.page_type, free_space_offset=len(chunk)).pack_into(buf)
        buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(chunk)] = chunk
        self.dm.write_page(page_id, bytes(buf))

    def close(self) -> None:
        if self._buf:
            self._emit(bytes(self._buf))
            self._buf = bytearray()


class _PageStreamReader:
    def __init__(self, dm: DiskManager, first_page: int) -> None:
        self.dm = dm
        self.page_id = first_page
        self._buf = b""
        self._pos = 0

    def read(self, n: int) -> bytes:
        while len(self._buf) - self._pos < n:
            data = self.dm.read_page(self.page_id)
            header = PageHeader.unpack(data)
            self.page_id += 1
            self._buf = self._buf[self._pos :] + data[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + header.free_space_offset]
            self._pos = 0
        out = self._buf[self._pos : self._pos + n]
        self._pos += n
        return out


# ---------------------------------------------------------------------- consultas booleanas
def intersect(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """``a AND b`` con dos punteros sobre listas ordenadas: O(n + m)."""
    out = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            i += 1
        else:
            j += 1
    return out


def union(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """``a OR b`` con dos punteros: O(n + m)."""
    out = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            out.append(y)
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def difference(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """``a AND NOT b`` con dos punteros: O(n + m)."""
    out = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            j += 1
    out.extend(a[i:])
    return out


@dataclass
class _Node:
    op: str  # 'term' | 'and' | 'or' | 'andnot' | 'not' | 'all'
    term: str | None = None
    left: "_Node | None" = None
    right: "_Node | None" = None


class _QueryParser:
    """``expr := conj {OR conj}``; ``conj := unit {[AND] [NOT] unit}``; ``unit := [NOT] (palabra | '(' expr ')')``."""

    def __init__(self, text: str, analyzer: Analyzer) -> None:
        self.tokens = _QUERY_TOKEN.findall(text)
        self.pos = 0
        self.analyzer = analyzer
        self.positive: list[str] = []  # términos que aportan al ranking (no negados)

    @property
    def is_boolean(self) -> bool:
        return any(t in ("AND", "OR", "NOT", "(", ")") for t in self.tokens)

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def parse(self) -> _Node | None:
        node = self._expr(negated=False)
        if self.pos < len(self.tokens):
            raise InvertedIndexError(f"consulta MATCH mal formada cerca de '{self.tokens[self.pos]}'")
        return node

    def _expr(self, negated: bool) -> _Node | None:
        node = self._conj(negated)
        while self._peek() == "OR":
            self.pos += 1
            right = self._conj(negated)
            node = right if node is None else node if right is None else _Node("or", left=node, right=right)
        return node

    def _conj(self, negated: bool) -> _Node | None:
        node, neg = self._unit(negated)
        if neg:
            node = None if node is None else _Node("not", left=node)
        while True:
            tok = self._peek()
            if tok is None or tok in ("OR", ")"):
                return node
            if tok == "AND":
                self.pos += 1
            right, rneg = self._unit(negated)
            if right is None:
                continue
            if rneg:
                node = _Node("andnot", left=node or _Node("all"), right=right)
            elif node is None:
                node = right
            else:
                node = _Node("and", left=node, right=right)

    def _unit(self, negated: bool) -> tuple[_Node | None, bool]:
        neg = False
        while self._peek() == "NOT":
            self.pos += 1
            neg = not neg
        tok = self._peek()
        if tok is None or tok in ("AND", "OR", ")"):
            raise InvertedIndexError("consulta MATCH mal formada: falta un término")
        self.pos += 1
        if tok == "(":
            node = self._expr(negated != neg)
            if self._peek() != ")":
                raise InvertedIndexError("consulta MATCH mal formada: falta ')'")
            self.pos += 1
            return node, neg
        terms = [_truncate(t) for t in self.analyzer.tokens(tok)]
        if not terms:  # stopword: elemento neutro
            return None, neg
        if negated == neg:
            self.positive.extend(terms)  # con repeticiones: es la frecuencia en la consulta
        node: _Node | None = None
        for term in dict.fromkeys(terms):  # "t-shirt" -> t AND shirt
            leaf = _Node("term", term=term)
            node = leaf if node is None else _Node("and", left=node, right=leaf)
        return node, neg


def compile_match(query: str, analyzer: Analyzer):
    """Compila una consulta ``MATCH`` a una función ``texto -> bool`` (búsqueda secuencial).

    Es la evaluación sin índice: tokeniza cada documento y comprueba la expresión
    booleana sobre su conjunto de términos. Una consulta de texto libre coincide
    si el documento contiene alguno de sus términos.
    """
    parser = _QueryParser(query, analyzer)
    boolean = parser.is_boolean
    tree = parser.parse()
    positive = set(parser.positive)

    def holds(node: _Node, terms: set[str]) -> bool:
        if node.op == "term":
            return node.term in terms
        if node.op == "all":
            return True
        if node.op == "not":
            return not holds(node.left, terms)  # type: ignore[arg-type]
        left = holds(node.left, terms)  # type: ignore[arg-type]
        if node.op == "and":
            return left and holds(node.right, terms)  # type: ignore[arg-type]
        if node.op == "or":
            return left or holds(node.right, terms)  # type: ignore[arg-type]
        return left and not holds(node.right, terms)  # type: ignore[arg-type]

    if tree is None:
        return lambda text: False
    if boolean:
        return lambda text: holds(tree, set(analyzer.term_frequencies(text)))
    return lambda text: not positive.isdisjoint(analyzer.term_frequencies(text))


@dataclass
class TermInfo:
    term: str
    df: int
    idf: float
    pages: int  # páginas de posteo de la lista

    def as_dict(self) -> dict[str, Any]:
        return {"term": self.term, "df": self.df, "idf": round(self.idf, 4), "posting_pages": self.pages}


@dataclass
class SearchResult:
    hits: list[tuple[RID, float]]
    total: int  # documentos que cumplen la consulta (antes del Top-K)
    mode: str  # 'boolean' | 'ranked'
    scoring: str
    terms: list[TermInfo] = field(default_factory=list)
    rebuilt: bool = False


# ---------------------------------------------------------------------- índice
class InvertedIndex:
    def __init__(self, base: Path, page_size: int, counter: DiskCounter, meta: dict[str, Any]) -> None:
        self.base = Path(base)
        self.page_size = page_size
        self.counter = counter
        self.vocab = DiskManager(self.vocab_path(base), page_size, counter)
        self.postings = DiskManager(self.postings_path(base), page_size, counter)
        self.docs = DiskManager(self.docs_path(base), page_size, counter)
        usable = page_size - PAGE_HEADER_SIZE
        self.vocab_per_page = usable // VOCAB_ENTRY.itemsize
        self.postings_per_page = usable // POSTING.itemsize
        self.docs_per_page = usable // DOC_STAT.itemsize
        self._load_meta(meta)
        self._meta_dirty = False

    def _load_meta(self, meta: dict[str, Any]) -> None:
        self.num_terms = int(meta["num_terms"])
        self.num_docs = int(meta["num_docs"])
        self.total_postings = int(meta["total_postings"])
        self.avg_length = float(meta["avg_length"])
        self.analyzer = Analyzer(ANALYZERS[int(meta["analyzer"])])
        self.scoring = SCORINGS[int(meta["scoring"])]
        self.stale = bool(meta["stale"])
        self.built = bool(meta["built"])

    # -- rutas
    @staticmethod
    def vocab_path(base: str | os.PathLike[str]) -> Path:
        return Path(str(base) + ".vocab.dic")

    @staticmethod
    def postings_path(base: str | os.PathLike[str]) -> Path:
        return Path(str(base) + ".postings.inv")

    @staticmethod
    def docs_path(base: str | os.PathLike[str]) -> Path:
        return Path(str(base) + ".docs.bin")

    @classmethod
    def paths(cls, base: str | os.PathLike[str]) -> list[Path]:
        return [cls.vocab_path(base), cls.postings_path(base), cls.docs_path(base)]

    # ------------------------------------------------------------------ apertura
    @classmethod
    def create(
        cls,
        base: str | os.PathLike[str],
        page_size: int,
        counter: DiskCounter,
        *,
        analyzer: str = "spanish",
        scoring: str = "TFIDF_COSINE",
    ) -> InvertedIndex:
        if analyzer not in ANALYZERS:
            raise InvertedIndexError(f"analizador desconocido '{analyzer}' (use {', '.join(ANALYZERS)})")
        if scoring not in SCORINGS:
            raise InvertedIndexError(f"scoring desconocido '{scoring}' (use {' o '.join(SCORINGS)})")
        meta = {
            "num_terms": 0, "num_docs": 0, "total_postings": 0, "avg_length": 0.0,
            "analyzer": ANALYZERS.index(analyzer), "scoring": SCORINGS.index(scoring), "stale": 0, "built": 0,
        }
        for path, codec, values in (
            (cls.vocab_path(base), VOCAB_META, meta),
            (cls.postings_path(base), POSTINGS_META, {"total_postings": 0}),
            (cls.docs_path(base), DOCS_META, {"num_docs": 0, "stat_pages": 0}),
        ):
            with DiskManager(path, page_size, counter, create=True) as dm:
                dm.write_page(dm.allocate_page(), codec.pack(page_size, 1, values))
        return cls(Path(base), page_size, counter, meta)

    @classmethod
    def open(cls, base: str | os.PathLike[str], page_size: int, counter: DiskCounter) -> InvertedIndex:
        with DiskManager(cls.vocab_path(base), page_size, counter) as dm:
            _, _, meta = VOCAB_META.unpack(dm.read_page(0))
        return cls(Path(base), page_size, counter, meta)

    def _vocab_meta_bytes(self) -> bytes:
        return VOCAB_META.pack(
            self.page_size,
            self.vocab.num_pages(),
            {
                "num_terms": self.num_terms, "num_docs": self.num_docs, "total_postings": self.total_postings,
                "avg_length": self.avg_length, "analyzer": ANALYZERS.index(self.analyzer.name),
                "scoring": SCORINGS.index(self.scoring), "stale": int(self.stale), "built": int(self.built),
            },
        )

    def mark_stale(self) -> None:
        """La tabla cambió: el índice se reconstruye antes de la próxima búsqueda."""
        if not self.stale:
            self.stale = True
            self._meta_dirty = True

    def flush(self) -> None:
        if self._meta_dirty:
            self.vocab.write_page(0, self._vocab_meta_bytes())
            self._meta_dirty = False

    def sync(self) -> None:
        self.flush()
        for dm in (self.vocab, self.postings, self.docs):
            dm.flush()

    def close(self) -> None:
        if not self.vocab.closed:
            self.sync()
        for dm in (self.vocab, self.postings, self.docs):
            dm.close()

    @property
    def page_count(self) -> int:
        return self.vocab.num_pages() + self.postings.num_pages() + self.docs.num_pages()

    @property
    def stat_pages(self) -> int:
        return math.ceil(self.num_docs / self.docs_per_page)

    def size_bytes(self) -> int:
        return self.page_count * self.page_size

    # ------------------------------------------------------------------ construcción (SPIMI)
    def build(
        self, documents: Iterator[tuple[RID, str]], block_postings: int = DEFAULT_BLOCK_POSTINGS
    ) -> dict[str, Any]:
        """(Re)construye los tres archivos recorriendo ``documents`` una sola vez."""
        t0 = time.perf_counter()
        for dm in (self.vocab, self.postings, self.docs):
            dm.truncate(1)
        runs_path = Path(str(self.base) + ".runs.tmp")
        runs_path.unlink(missing_ok=True)
        runs = DiskManager(runs_path, self.page_size, self.counter, create=True)
        run_starts: list[tuple[int, int]] = []  # (primera página, nº de términos)
        block: dict[str, tuple[array, array]] = {}
        in_block = 0
        rid_pages = array("i")
        rid_slots = array("H")
        lengths = array("I")
        analyze = self.analyzer.term_frequencies

        def flush_block() -> None:
            nonlocal block, in_block
            if not block:
                return
            writer = _PageStreamWriter(runs, PageType.SORT_RUN)
            for term in sorted(block, key=lambda t: t.encode("utf-8")):
                docs_arr, tf_arr = block[term]
                raw = term.encode("utf-8")
                postings = np.empty(len(docs_arr), dtype=POSTING)
                postings["doc"] = docs_arr
                postings["tf"] = tf_arr
                writer.write(_RUN_HEAD.pack(len(raw), len(docs_arr)) + raw + postings.tobytes())
            writer.close()
            run_starts.append((writer.first_page, len(block)))
            block = {}
            in_block = 0

        try:
            doc_id = 0
            for rid, text in documents:
                tf = analyze(text)
                rid_pages.append(rid.page_id)
                rid_slots.append(rid.slot)
                lengths.append(min(sum(tf.values()), 0xFFFFFFFF))
                for term, count in tf.items():
                    if len(term) > TERM_SIZE // 4:
                        term = _truncate(term)
                    entry = block.get(term)
                    if entry is None:
                        entry = block[term] = (array("I"), array("H"))
                    if entry[0] and entry[0][-1] == doc_id:  # dos términos largos con el mismo prefijo
                        entry[1][-1] = min(entry[1][-1] + count, 0xFFFF)
                        continue
                    entry[0].append(doc_id)
                    entry[1].append(min(count, 0xFFFF))
                in_block += len(tf)
                doc_id += 1
                if in_block >= block_postings:
                    flush_block()
            flush_block()
            n_docs = doc_id
            norm_sq = np.zeros(n_docs, dtype=np.float64)
            num_terms, total_postings = self._merge_runs(runs, run_starts, n_docs, norm_sq)
        finally:
            runs.close()
            runs_path.unlink(missing_ok=True)

        self._write_docs(np.sqrt(norm_sq), lengths, rid_pages, rid_slots)
        self.num_terms = num_terms
        self.num_docs = n_docs
        self.total_postings = total_postings
        self.avg_length = (sum(lengths) / n_docs) if n_docs else 0.0
        self.stale = False
        self.built = True
        self.vocab.write_page(0, self._vocab_meta_bytes())
        self.postings.write_page(
            0, POSTINGS_META.pack(self.page_size, self.postings.num_pages(), {"total_postings": total_postings})
        )
        self._meta_dirty = False
        return {
            "documents": n_docs,
            "terms": num_terms,
            "postings": total_postings,
            "runs": len(run_starts),
            "build_ms": round((time.perf_counter() - t0) * 1000, 3),
            "size_bytes": self.size_bytes(),
        }

    def _run_iter(self, runs: DiskManager, first_page: int, terms: int, order: int) -> Iterator[tuple[bytes, int, bytes]]:
        reader = _PageStreamReader(runs, first_page)
        for _ in range(terms):
            term_len, count = _RUN_HEAD.unpack(reader.read(_RUN_HEAD.size))
            term = reader.read(term_len)
            yield term, order, reader.read(count * POSTING.itemsize)

    def _merge_runs(
        self, runs: DiskManager, run_starts: list[tuple[int, int]], n_docs: int, norm_sq: np.ndarray
    ) -> tuple[int, int]:
        """Mezcla de k vías: escribe ``postings.inv`` y ``vocab.dic`` en orden de término."""
        streams = [self._run_iter(runs, first, terms, i) for i, (first, terms) in enumerate(run_starts)]
        merged = heapq.merge(*streams, key=lambda item: (item[0], item[1]))
        post_buf = bytearray()
        post_page_bytes = self.postings_per_page * POSTING.itemsize
        vocab_buf = np.zeros(self.vocab_per_page, dtype=VOCAB_ENTRY)
        vocab_n = 0
        num_terms = 0
        offset = 0

        def emit_postings(final: bool = False) -> None:
            nonlocal post_buf
            while len(post_buf) >= post_page_bytes or (final and post_buf):
                chunk = bytes(post_buf[:post_page_bytes])
                del post_buf[:post_page_bytes]
                page_id = self.postings.allocate_page()
                buf = bytearray(self.page_size)
                PageHeader(page_id=page_id, page_type=PageType.INV_POSTINGS,
                           record_count=len(chunk) // POSTING.itemsize,
                           free_space_offset=PAGE_HEADER_SIZE + len(chunk)).pack_into(buf)
                buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(chunk)] = chunk
                self.postings.write_page(page_id, bytes(buf))

        def emit_vocab() -> None:
            nonlocal vocab_n
            if not vocab_n:
                return
            page_id = self.vocab.allocate_page()
            raw = vocab_buf[:vocab_n].tobytes()
            buf = bytearray(self.page_size)
            PageHeader(page_id=page_id, page_type=PageType.INV_VOCAB, record_count=vocab_n,
                       free_space_offset=PAGE_HEADER_SIZE + len(raw)).pack_into(buf)
            buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(raw)] = raw
            self.vocab.write_page(page_id, bytes(buf))
            vocab_n = 0

        def finish_term(term: bytes, parts: list[bytes]) -> None:
            nonlocal offset, vocab_n, num_terms
            raw = b"".join(parts)
            postings = np.frombuffer(raw, dtype=POSTING)
            df = len(postings)
            if n_docs and df:
                idf = math.log10(n_docs / df)
                w = (1.0 + np.log10(postings["tf"].astype(np.float64))) * idf
                norm_sq[postings["doc"]] += w * w
            post_buf.extend(raw)
            emit_postings()
            vocab_buf[vocab_n] = (term, df, df, offset)
            vocab_n += 1
            if vocab_n == self.vocab_per_page:
                emit_vocab()
            offset += df
            num_terms += 1

        current: bytes | None = None
        parts: list[bytes] = []
        for term, _, raw in merged:
            if term != current:
                if current is not None:
                    finish_term(current, parts)
                current, parts = term, []
            parts.append(raw)
        if current is not None:
            finish_term(current, parts)
        emit_postings(final=True)
        emit_vocab()
        return num_terms, offset

    def _write_docs(self, norms: np.ndarray, lengths: array, rid_pages: array, rid_slots: array) -> None:
        n = len(norms)
        per = self.docs_per_page
        stats = np.zeros(n, dtype=DOC_STAT)
        stats["norm"] = norms
        stats["length"] = lengths
        rids = np.zeros(n, dtype=DOC_RID)
        rids["page"] = rid_pages
        rids["slot"] = rid_slots
        for arr in (stats, rids):
            for start in range(0, n, per):
                chunk = arr[start : start + per]
                page_id = self.docs.allocate_page()
                raw = chunk.tobytes()
                buf = bytearray(self.page_size)
                PageHeader(page_id=page_id, page_type=PageType.INV_DOCS, record_count=len(chunk),
                           free_space_offset=PAGE_HEADER_SIZE + len(raw)).pack_into(buf)
                buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(raw)] = raw
                self.docs.write_page(page_id, bytes(buf))
        self.docs.write_page(
            0, DOCS_META.pack(self.page_size, self.docs.num_pages(), {"num_docs": n, "stat_pages": math.ceil(n / per)})
        )

    # ------------------------------------------------------------------ lectura
    def _vocab_page(self, page_id: int) -> np.ndarray:
        data = self.vocab.read_page(page_id)
        header = PageHeader.unpack(data)
        expect_page_type(header, PageType.INV_VOCAB)
        return np.frombuffer(data, dtype=VOCAB_ENTRY, count=header.record_count, offset=PAGE_HEADER_SIZE)

    def lookup(self, term: str) -> tuple[int, int] | None:
        """``(df, offset)`` del término con búsqueda binaria sobre las páginas del diccionario."""
        key = _truncate(term).encode("utf-8")
        lo, hi = 1, self.vocab.num_pages() - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            entries = self._vocab_page(mid)
            terms = entries["term"]
            if key < terms[0]:
                hi = mid - 1
            elif key > terms[-1]:
                lo = mid + 1
            else:
                i = int(np.searchsorted(terms, np.bytes_(key)))
                if i < len(terms) and terms[i] == key:
                    return int(entries["df"][i]), int(entries["offset"][i])
                return None
        return None

    def read_postings(self, df: int, offset: int) -> np.ndarray:
        """Lee solo las páginas que ocupa la lista (``seek`` al offset del diccionario)."""
        per = self.postings_per_page
        first, last = offset // per, (offset + df - 1) // per
        chunks = []
        for p in range(first, last + 1):
            data = self.postings.read_page(1 + p)
            header = PageHeader.unpack(data)
            page = np.frombuffer(data, dtype=POSTING, count=header.record_count, offset=PAGE_HEADER_SIZE)
            start = offset - p * per if p == first else 0
            end = offset + df - p * per if p == last else per
            chunks.append(page[start:end])
        return chunks[0] if len(chunks) == 1 else np.concatenate(chunks)

    def _doc_stats(self, doc_ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Norma y longitud de ``doc_ids`` leyendo solo sus páginas de estadísticas."""
        per = self.docs_per_page
        norms = np.empty(len(doc_ids), dtype=np.float64)
        lengths = np.empty(len(doc_ids), dtype=np.float64)
        pages = doc_ids // per
        for p in np.unique(pages):
            data = self.docs.read_page(1 + int(p))
            header = PageHeader.unpack(data)
            arr = np.frombuffer(data, dtype=DOC_STAT, count=header.record_count, offset=PAGE_HEADER_SIZE)
            mask = pages == p
            local = doc_ids[mask] - int(p) * per
            norms[mask] = arr["norm"][local]
            lengths[mask] = arr["length"][local]
        return norms, lengths

    def _doc_rids(self, doc_ids: Sequence[int]) -> list[RID]:
        per = self.docs_per_page
        base = 1 + self.stat_pages
        cache: dict[int, np.ndarray] = {}
        out = []
        for d in doc_ids:
            p = int(d) // per
            arr = cache.get(p)
            if arr is None:
                data = self.docs.read_page(base + p)
                header = PageHeader.unpack(data)
                arr = cache[p] = np.frombuffer(data, dtype=DOC_RID, count=header.record_count,
                                               offset=PAGE_HEADER_SIZE)
            row = arr[int(d) - p * per]
            out.append(RID(int(row["page"]), int(row["slot"])))
        return out

    # ------------------------------------------------------------------ búsqueda
    def search(
        self,
        query: str,
        k: int | None = None,
        *,
        ranked: bool = True,
        allowed: set[RID] | None = None,
    ) -> SearchResult:
        """Evalúa ``query`` (booleana si usa AND / OR / NOT / paréntesis; si no, texto libre).

        ``ranked``: calcula la puntuación y ordena por relevancia (Top-``k``).
        ``allowed``: pre-filtro; solo se devuelven documentos cuyo RID está en el conjunto.
        """
        if self.stale or not self.built:
            raise InvertedIndexError("el índice invertido está desactualizado y debe reconstruirse")
        parser = _QueryParser(query, self.analyzer)
        boolean = parser.is_boolean
        tree = parser.parse()
        cache: dict[str, tuple[int, np.ndarray] | None] = {}
        infos: list[TermInfo] = []
        n = self.num_docs

        def postings_of(term: str) -> tuple[int, np.ndarray] | None:
            if term not in cache:
                found = self.lookup(term)
                if found is None:
                    cache[term] = None
                    infos.append(TermInfo(term, 0, 0.0, 0))
                else:
                    df, offset = found
                    per = self.postings_per_page
                    cache[term] = (df, self.read_postings(df, offset))
                    infos.append(TermInfo(term, df, math.log10(n / df) if df else 0.0,
                                          (offset + df - 1) // per - offset // per + 1))
            return cache[term]

        def evaluate(node: _Node) -> list[int]:
            if node.op == "term":
                found = postings_of(node.term or "")
                return [] if found is None else found[1]["doc"].tolist()
            if node.op == "all":
                return list(range(n))
            if node.op == "not":
                return difference(range(n), evaluate(node.left))  # type: ignore[arg-type]
            left = evaluate(node.left)  # type: ignore[arg-type]
            right = evaluate(node.right)  # type: ignore[arg-type]
            if node.op == "and":
                return intersect(left, right)
            if node.op == "or":
                return union(left, right)
            return difference(left, right)

        mode = "boolean" if boolean else "ranked"
        if tree is None:
            return SearchResult([], 0, mode, self.scoring, infos)
        if boolean:
            candidates = np.asarray(evaluate(tree), dtype=np.int64)
            terms = list(dict.fromkeys(parser.positive))
        else:
            terms = list(dict.fromkeys(parser.positive))
            for t in terms:
                postings_of(t)
            lists = [cache[t][1]["doc"].tolist() for t in terms if cache.get(t)]  # type: ignore[index]
            merged: list[int] = []
            for lst in sorted(lists, key=len):
                merged = union(merged, lst)
            candidates = np.asarray(merged, dtype=np.int64)
        if len(candidates) == 0:
            return SearchResult([], 0, mode, self.scoring, infos)
        if not ranked:
            rids = self._doc_rids(candidates.tolist())
            hits = [(r, 0.0) for r in rids if allowed is None or r in allowed]
            total = len(hits)
            return SearchResult(hits if k is None else hits[:k], total, mode, self.scoring, infos)

        scores = self._score(terms, parser.positive, cache, candidates)
        if allowed is not None:
            rids = self._doc_rids(candidates.tolist())
            keep = np.fromiter((r in allowed for r in rids), dtype=bool, count=len(rids))
            candidates, scores = candidates[keep], scores[keep]
            if len(candidates) == 0:
                return SearchResult([], 0, mode, self.scoring, infos)
        total = len(candidates)
        if k is None or k >= total:
            order = np.lexsort((candidates, -scores))
        else:
            # Top-K con un heap: O(n log k)
            top = heapq.nlargest(k, zip(scores.tolist(), (-candidates).tolist()))
            pos = {int(d): i for i, d in enumerate(candidates.tolist())}
            order = np.asarray([pos[-d] for _, d in top], dtype=np.int64)
        top_docs = candidates[order]
        rids = self._doc_rids(top_docs.tolist())
        return SearchResult(list(zip(rids, scores[order].tolist())), total, mode, self.scoring, infos)

    def _score(
        self,
        terms: list[str],
        query_terms: list[str],
        cache: dict[str, tuple[int, np.ndarray] | None],
        candidates: np.ndarray,
    ) -> np.ndarray:
        """Puntuación de ``candidates`` acumulando solo sobre las listas de los términos de la consulta."""
        n = self.num_docs
        acc = np.zeros(n, dtype=np.float64)
        qtf: dict[str, int] = {}
        for t in query_terms:
            qtf[t] = qtf.get(t, 0) + 1
        if self.scoring == "BM25":
            _, lengths_all = self._doc_stats(candidates)
            length = np.zeros(n, dtype=np.float64)
            length[candidates] = lengths_all
            avg = self.avg_length or 1.0
            for t in terms:
                found = cache.get(t)
                if not found:
                    continue
                df, postings = found
                idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                tf = postings["tf"].astype(np.float64)
                docs = postings["doc"]
                denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * length[docs] / avg)
                acc[docs] += qtf[t] * idf * tf * (BM25_K1 + 1.0) / denom
            return acc[candidates]
        q_norm_sq = 0.0
        for t in terms:
            found = cache.get(t)
            if not found:
                continue
            df, postings = found
            idf = math.log10(n / df)
            wq = (1.0 + math.log10(qtf[t])) * idf
            q_norm_sq += wq * wq
            acc[postings["doc"]] += wq * (1.0 + np.log10(postings["tf"].astype(np.float64))) * idf
        norms, _ = self._doc_stats(candidates)
        q_norm = math.sqrt(q_norm_sq)
        scores = acc[candidates]
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where((norms > 0) & (q_norm > 0), scores / (norms * q_norm), 0.0)
        return scores

    # ------------------------------------------------------------------ inspección
    def stats(self) -> dict[str, Any]:
        return {
            "terms": self.num_terms,
            "documents": self.num_docs,
            "postings": self.total_postings,
            "avg_length": round(self.avg_length, 2),
            "analyzer": self.analyzer.name,
            "scoring": self.scoring,
            "stale": self.stale,
            "vocab_pages": self.vocab.num_pages(),
            "postings_pages": self.postings.num_pages(),
            "docs_pages": self.docs.num_pages(),
        }

    def iter_vocab(self) -> Iterator[tuple[str, int, int]]:
        for page_id in range(1, self.vocab.num_pages()):
            for entry in self._vocab_page(page_id):
                yield entry["term"].decode("utf-8", errors="ignore"), int(entry["df"]), int(entry["offset"])
