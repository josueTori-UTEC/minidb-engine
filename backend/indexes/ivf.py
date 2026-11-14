"""Índice vectorial aproximado IVF-Flat (*Inverted File*) en disco.

El espacio ``R^D`` se particiona en ``C`` celdas de Voronoi con K-Means
(por defecto ``C = ⌊√N⌋``). Dos archivos binarios paginados (``base`` =
``<tabla>_<col>``):

``base.ivf.dir`` — **directorio de centroides**
    Página 0: metadatos (``dims``, métrica, nº de listas, nº de vectores,
    bandera ``trained``). Páginas 1.. (``IVF_DIR``): entradas
    ``<cluster_id (u32), centroide (D·f32), head_page_id (i32), count (u32), pages (u32)>``
    = ``16 + 4D`` bytes.

``base.ivf.lst`` — **listas invertidas paginadas**
    Cada celda es una cadena de páginas ``IVF_LIST`` enlazadas por
    ``next_page_id`` (``aux_page_id`` = ``cluster_id``); una página guarda tuplas
    ``<RID (6 B), vector (D·f32)>`` = ``6 + 4D`` bytes (7 por página de 4 KB con
    ``D = 128``).

**Construcción.** Dos pasadas sobre la tabla: (1) se toma una muestra por
*reservoir sampling* (hasta ``max_train`` vectores) y se entrena K-Means;
(2) cada vector se asigna a su centroide más cercano y se agrega a la página en
curso de esa celda. En memoria solo hay una página por celda, nunca la tabla.

**Búsqueda** (``probes`` celdas): se leen las páginas del directorio, se eligen
los ``probes`` centroides más cercanos a la consulta y se recorren únicamente las
cadenas de páginas de esas celdas; las distancias exactas a los vectores leídos
dan los candidatos ordenados. Con ``probes = C`` la búsqueda es exacta.

Métricas: ``L2`` (distancia euclidiana, operador ``<->``) y ``COSINE``
(``1 - cos θ``, operador ``<=>``); con coseno los centroides se normalizan
(K-Means esférico). La aritmética usa ``numpy`` sobre los bytes de las páginas;
todo acceso a disco pasa por ``DiskManager``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import numpy as np

from backend.storage.disk_manager import DiskCounter, DiskManager
from backend.storage.page import (
    NULL_PAGE,
    PAGE_HEADER_SIZE,
    FileKind,
    MetaCodec,
    PageFormatError,
    PageHeader,
    PageType,
    expect_page_type,
)
from backend.storage.rid import RID

METRICS = ("L2", "COSINE")
DEFAULT_MAX_TRAIN = 50_000
KMEANS_ITERATIONS = 20

DIR_META = MetaCodec(
    FileKind.IVF_DIR,
    [("dims", "H"), ("metric", "B"), ("trained", "B"), ("num_lists", "I"), ("entry_count", "Q"),
     ("target_lists", "I")],
)
LIST_META = MetaCodec(FileKind.IVF_LIST, [("dims", "H")])

VectorSource = Callable[[], Iterator[tuple[RID, Sequence[float]]]]


class IVFError(Exception):
    """Parámetros inválidos del índice IVF."""


# ---------------------------------------------------------------------- distancias y K-Means
def distances(metric: str, query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    """Distancia de ``query`` a cada fila de ``vectors`` (``L2`` o ``1 - cos``)."""
    if metric == "L2":
        diff = vectors - query
        return np.sqrt(np.einsum("ij,ij->i", diff, diff))
    qn = float(np.linalg.norm(query))
    vn = np.linalg.norm(vectors, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cos = np.where((vn > 0) & (qn > 0), (vectors @ query) / (vn * qn), 0.0)
    return 1.0 - cos


def _assign(metric: str, data: np.ndarray, centroids: np.ndarray, chunk: int = 4096) -> np.ndarray:
    """Índice del centroide más cercano a cada fila de ``data`` (por bloques)."""
    out = np.empty(len(data), dtype=np.int64)
    if metric == "COSINE":
        for s in range(0, len(data), chunk):
            out[s : s + chunk] = np.argmax(data[s : s + chunk] @ centroids.T, axis=1)
        return out
    c_sq = np.einsum("ij,ij->i", centroids, centroids)
    for s in range(0, len(data), chunk):
        block = data[s : s + chunk]
        # ||x - c||² = ||x||² - 2 x·c + ||c||²  (||x||² no cambia el argmin)
        out[s : s + chunk] = np.argmin(c_sq - 2.0 * (block @ centroids.T), axis=1)
    return out


def _normalize_rows(a: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.where(norms > 0, norms, 1.0)


def kmeans(data: np.ndarray, k: int, metric: str = "L2", iterations: int = KMEANS_ITERATIONS,
           seed: int = 42) -> np.ndarray:
    """K-Means de Lloyd con inicialización k-means++ (esférico si la métrica es coseno)."""
    n = len(data)
    if n == 0:
        raise IVFError("no hay vectores para entrenar K-Means")
    k = max(1, min(k, n))
    x = data.astype(np.float32)
    if metric == "COSINE":
        x = _normalize_rows(x)
    rng = np.random.default_rng(seed)
    # k-means++: cada centro nuevo se elige con probabilidad proporcional a D(x)²
    centroids = np.empty((k, x.shape[1]), dtype=np.float32)
    centroids[0] = x[rng.integers(n)]
    closest = np.full(n, np.inf)
    for i in range(1, k):
        diff = x - centroids[i - 1]
        closest = np.minimum(closest, np.einsum("ij,ij->i", diff, diff))
        total = float(closest.sum())
        centroids[i] = x[rng.integers(n)] if total <= 0 else x[rng.choice(n, p=closest / total)]
    for _ in range(iterations):
        labels = _assign(metric, x, centroids)
        sums = np.zeros_like(centroids, dtype=np.float64)
        np.add.at(sums, labels, x)
        counts = np.bincount(labels, minlength=k)
        fresh = centroids.copy()
        nonempty = counts > 0
        fresh[nonempty] = (sums[nonempty] / counts[nonempty, None]).astype(np.float32)
        empty = np.flatnonzero(~nonempty)
        if len(empty):  # celdas vacías: se reubican en puntos al azar
            fresh[empty] = x[rng.choice(n, size=len(empty), replace=n < len(empty))]
        if metric == "COSINE":
            fresh = _normalize_rows(fresh)
        moved = float(np.abs(fresh - centroids).max())
        centroids = fresh
        if moved < 1e-5:
            break
    return centroids


# ---------------------------------------------------------------------- índice
class IVFIndex:
    def __init__(self, base: Path, page_size: int, counter: DiskCounter, meta: dict[str, Any]) -> None:
        self.base = Path(base)
        self.page_size = page_size
        self.counter = counter
        self.dir = DiskManager(self.dir_path(base), page_size, counter)
        self.lists = DiskManager(self.lists_path(base), page_size, counter)
        self.dims = int(meta["dims"])
        self.metric = METRICS[int(meta["metric"])]
        self.trained = bool(meta["trained"])
        self.num_lists = int(meta["num_lists"])
        self.entry_count = int(meta["entry_count"])
        self.target_lists = int(meta["target_lists"])
        d = self.dims
        self._dir_dtype = np.dtype(
            {"names": ["cluster", "centroid", "head", "count", "pages"],
             "formats": ["<u4", ("<f4", (d,)), "<i4", "<u4", "<u4"]}
        )
        self._entry_dtype = np.dtype({"names": ["page", "slot", "vector"], "formats": ["<i4", "<u2", ("<f4", (d,))]})
        usable = page_size - PAGE_HEADER_SIZE
        self.dir_per_page = usable // self._dir_dtype.itemsize
        self.list_capacity = usable // self._entry_dtype.itemsize
        if self.dir_per_page < 1 or self.list_capacity < 1:
            raise PageFormatError(f"un vector de {d} dimensiones no cabe en una página de {page_size} bytes")
        self._meta_dirty = False

    @staticmethod
    def dir_path(base: str | os.PathLike[str]) -> Path:
        return Path(str(base) + ".ivf.dir")

    @staticmethod
    def lists_path(base: str | os.PathLike[str]) -> Path:
        return Path(str(base) + ".ivf.lst")

    @classmethod
    def paths(cls, base: str | os.PathLike[str]) -> list[Path]:
        return [cls.dir_path(base), cls.lists_path(base)]

    # ------------------------------------------------------------------ apertura
    @classmethod
    def create(
        cls,
        base: str | os.PathLike[str],
        dims: int,
        page_size: int,
        counter: DiskCounter,
        *,
        metric: str = "L2",
        lists: int = 0,
    ) -> IVFIndex:
        """Crea un índice vacío con una sola celda; ``build`` lo entrena.

        ``lists = 0`` significa ``⌊√N⌋`` celdas al entrenar.
        """
        if metric not in METRICS:
            raise IVFError(f"métrica desconocida '{metric}' (use {' o '.join(METRICS)})")
        if lists < 0:
            raise IVFError("lists debe ser >= 0")
        meta = {"dims": dims, "metric": METRICS.index(metric), "trained": 0, "num_lists": 0, "entry_count": 0,
                "target_lists": lists}
        with DiskManager(cls.dir_path(base), page_size, counter, create=True) as dm:
            dm.write_page(dm.allocate_page(), DIR_META.pack(page_size, 1, meta))
        with DiskManager(cls.lists_path(base), page_size, counter, create=True) as dm:
            dm.write_page(dm.allocate_page(), LIST_META.pack(page_size, 1, {"dims": dims}))
        index = cls(Path(base), page_size, counter, meta)
        index._write_directory(np.zeros((1, dims), dtype=np.float32), [NULL_PAGE], [0], [0])
        index.num_lists = 1
        index.dir.write_page(0, index._meta_bytes())
        return index

    @classmethod
    def open(cls, base: str | os.PathLike[str], page_size: int, counter: DiskCounter) -> IVFIndex:
        with DiskManager(cls.dir_path(base), page_size, counter) as dm:
            _, _, meta = DIR_META.unpack(dm.read_page(0))
        return cls(Path(base), page_size, counter, meta)

    def _meta_bytes(self) -> bytes:
        return DIR_META.pack(
            self.page_size,
            self.dir.num_pages(),
            {"dims": self.dims, "metric": METRICS.index(self.metric), "trained": int(self.trained),
             "num_lists": self.num_lists, "entry_count": self.entry_count, "target_lists": self.target_lists},
        )

    def flush(self) -> None:
        if self._meta_dirty:
            self.dir.write_page(0, self._meta_bytes())
            self._meta_dirty = False

    def sync(self) -> None:
        self.flush()
        self.dir.flush()
        self.lists.flush()

    def close(self) -> None:
        if not self.dir.closed:
            self.sync()
        self.dir.close()
        self.lists.close()

    @property
    def page_count(self) -> int:
        return self.dir.num_pages() + self.lists.num_pages()

    @property
    def dir_pages(self) -> int:
        return math.ceil(self.num_lists / self.dir_per_page)

    @property
    def needs_training(self) -> bool:
        """Índice creado sobre una tabla vacía que ya tiene datos suficientes para particionar."""
        return not self.trained and self.entry_count >= 64

    # ------------------------------------------------------------------ directorio
    def _write_directory(
        self, centroids: np.ndarray, heads: Sequence[int], counts: Sequence[int], pages: Sequence[int]
    ) -> None:
        c = len(centroids)
        entries = np.zeros(c, dtype=self._dir_dtype)
        entries["cluster"] = np.arange(c)
        entries["centroid"] = centroids
        entries["head"] = heads
        entries["count"] = counts
        entries["pages"] = pages
        self.dir.truncate(1)
        for start in range(0, c, self.dir_per_page):
            self._write_dir_page(self.dir.allocate_page(), entries[start : start + self.dir_per_page])

    def _write_dir_page(self, page_id: int, entries: np.ndarray) -> None:
        raw = entries.tobytes()
        buf = bytearray(self.page_size)
        PageHeader(page_id=page_id, page_type=PageType.IVF_DIR, record_count=len(entries),
                   free_space_offset=PAGE_HEADER_SIZE + len(raw)).pack_into(buf)
        buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(raw)] = raw
        self.dir.write_page(page_id, bytes(buf))

    def _read_dir_page(self, page_id: int) -> np.ndarray:
        data = self.dir.read_page(page_id)
        header = PageHeader.unpack(data)
        expect_page_type(header, PageType.IVF_DIR)
        return np.frombuffer(data, dtype=self._dir_dtype, count=header.record_count, offset=PAGE_HEADER_SIZE)

    def read_directory(self) -> np.ndarray:
        """Todas las entradas del directorio (una lectura por página de directorio)."""
        parts = [self._read_dir_page(p) for p in range(1, self.dir.num_pages())]
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def _nearest_lists(self, directory: np.ndarray, query: np.ndarray, probes: int) -> np.ndarray:
        dist = distances(self.metric, query, directory["centroid"])
        probes = max(1, min(probes, len(directory)))
        if probes >= len(directory):
            return np.argsort(dist, kind="stable")
        part = np.argpartition(dist, probes - 1)[:probes]
        return part[np.argsort(dist[part], kind="stable")]

    # ------------------------------------------------------------------ páginas de listas
    def _read_list_page(self, page_id: int) -> tuple[PageHeader, np.ndarray]:
        data = self.lists.read_page(page_id)
        header = PageHeader.unpack(data)
        expect_page_type(header, PageType.IVF_LIST)
        return header, np.frombuffer(data, dtype=self._entry_dtype, count=header.record_count,
                                     offset=PAGE_HEADER_SIZE)

    def _write_list_page(self, page_id: int, cluster: int, next_page: int, entries: np.ndarray) -> None:
        raw = entries.tobytes()
        buf = bytearray(self.page_size)
        PageHeader(page_id=page_id, page_type=PageType.IVF_LIST, record_count=len(entries),
                   free_space_offset=PAGE_HEADER_SIZE + len(raw), next_page_id=next_page,
                   aux_page_id=cluster).pack_into(buf)
        buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(raw)] = raw
        self.lists.write_page(page_id, bytes(buf))

    def _vector(self, values: Sequence[float]) -> np.ndarray:
        vec = np.asarray(values, dtype=np.float32)
        if vec.shape != (self.dims,):
            raise IVFError(f"el vector tiene {vec.size} componentes y el índice es de {self.dims}")
        return vec

    # ------------------------------------------------------------------ construcción
    def build(self, source: VectorSource, *, lists: int | None = None, max_train: int = DEFAULT_MAX_TRAIN,
              seed: int = 42) -> dict[str, Any]:
        """Entrena K-Means sobre una muestra y reparte todos los vectores en sus celdas."""
        rng = np.random.default_rng(seed)
        sample = np.empty((0, self.dims), dtype=np.float32)
        buf: list[np.ndarray] = []
        n = 0
        # pasada 1: reservoir sampling
        for _, values in source():
            vec = self._vector(values)
            if n < max_train:
                buf.append(vec)
            else:
                j = int(rng.integers(n + 1))
                if j < max_train:
                    buf[j] = vec
            n += 1
        if buf:
            sample = np.vstack(buf)
        target = lists if lists else self.target_lists
        c = target if target else max(1, int(math.sqrt(n)))
        c = max(1, min(c, max(1, n)))
        centroids = kmeans(sample, c, self.metric, seed=seed) if n else np.zeros((1, self.dims), dtype=np.float32)
        c = len(centroids)

        # pasada 2: asignación en streaming, una página en curso por celda
        self.lists.truncate(1)
        cap = self.list_capacity
        current = [np.zeros(cap, dtype=self._entry_dtype) for _ in range(c)]
        fill = [0] * c
        heads = [NULL_PAGE] * c
        counts = [0] * c
        pages = [0] * c

        def flush_cell(i: int) -> None:
            page_id = self.lists.allocate_page()
            self._write_list_page(page_id, i, heads[i], current[i][: fill[i]])
            heads[i] = page_id
            pages[i] += 1
            fill[i] = 0

        batch_rids: list[RID] = []
        batch_vecs: list[np.ndarray] = []

        def flush_batch() -> None:
            if not batch_vecs:
                return
            mat = np.vstack(batch_vecs)
            norm = _normalize_rows(mat) if self.metric == "COSINE" else mat
            labels = _assign(self.metric, norm, centroids)
            for rid, vec, label in zip(batch_rids, batch_vecs, labels.tolist()):
                entry = current[label][fill[label]]
                entry["page"], entry["slot"], entry["vector"] = rid.page_id, rid.slot, vec
                fill[label] += 1
                counts[label] += 1
                if fill[label] == cap:
                    flush_cell(label)
            batch_rids.clear()
            batch_vecs.clear()

        total = 0
        for rid, values in source():
            batch_rids.append(rid)
            batch_vecs.append(self._vector(values))
            total += 1
            if len(batch_vecs) >= 2048:
                flush_batch()
        flush_batch()
        for i in range(c):
            if fill[i]:
                flush_cell(i)
        self._write_directory(centroids, heads, counts, pages)
        self.num_lists = c
        self.entry_count = total
        self.trained = total > 0
        self.dir.write_page(0, self._meta_bytes())
        self._meta_dirty = False
        return {
            "vectors": total,
            "lists": c,
            "trained_on": len(sample),
            "list_pages": self.lists.num_pages() - 1,
            "dir_pages": self.dir.num_pages() - 1,
            "avg_list": round(total / c, 1) if c else 0,
        }

    # ------------------------------------------------------------------ inserción y borrado
    def _locate(self, cluster: int) -> tuple[int, int]:
        return 1 + cluster // self.dir_per_page, cluster % self.dir_per_page

    def insert(self, values: Sequence[float], rid: RID) -> None:
        """Agrega el vector a la celda de su centroide más cercano (cabeza de la cadena)."""
        vec = self._vector(values)
        directory = self.read_directory()
        cluster = int(self._nearest_lists(directory, vec, 1)[0])
        page_id, pos = self._locate(cluster)
        entries = self._read_dir_page(page_id).copy()
        head = int(entries["head"][pos])
        new = np.zeros(1, dtype=self._entry_dtype)
        new["page"], new["slot"], new["vector"] = rid.page_id, rid.slot, vec
        appended = False
        if head != NULL_PAGE:
            header, page = self._read_list_page(head)
            if header.record_count < self.list_capacity:
                self._write_list_page(head, cluster, header.next_page_id, np.concatenate([page, new]))
                appended = True
        if not appended:  # celda vacía o cabeza llena: página nueva al frente de la cadena
            fresh = self.lists.allocate_page()
            self._write_list_page(fresh, cluster, head, new)
            entries["head"][pos] = fresh
            entries["pages"][pos] += 1
        entries["count"][pos] += 1
        self._write_dir_page(page_id, entries)
        self.entry_count += 1
        self._meta_dirty = True

    def delete(self, values: Sequence[float], rid: RID) -> bool:
        """Quita ``<rid, vector>``: busca primero en la celda más cercana y luego en las demás."""
        vec = self._vector(values)
        directory = self.read_directory()
        order = self._nearest_lists(directory, vec, len(directory))
        for cluster in order.tolist():
            page_id = int(directory["head"][cluster])
            while page_id != NULL_PAGE:
                header, page = self._read_list_page(page_id)
                hit = np.flatnonzero((page["page"] == rid.page_id) & (page["slot"] == rid.slot))
                if len(hit):
                    keep = np.delete(page, int(hit[0]))
                    self._write_list_page(page_id, cluster, header.next_page_id, keep)
                    dir_page, pos = self._locate(cluster)
                    entries = self._read_dir_page(dir_page).copy()
                    entries["count"][pos] -= 1
                    self._write_dir_page(dir_page, entries)
                    self.entry_count -= 1
                    self._meta_dirty = True
                    return True
                page_id = header.next_page_id
        return False

    # ------------------------------------------------------------------ búsqueda
    def search(self, query: Sequence[float], k: int | None = None, probes: int = 1) -> dict[str, Any]:
        """Candidatos de las ``probes`` celdas más cercanas, ordenados por distancia.

        Devuelve ``{"hits": [(RID, distancia), ...], "probed": [...], "scanned": n, ...}``;
        con ``k = None`` entrega todos los candidatos (para el post-filtrado).
        """
        q = self._vector(query)
        directory = self.read_directory()
        chosen = self._nearest_lists(directory, q, probes)
        rid_pages: list[np.ndarray] = []
        rid_slots: list[np.ndarray] = []
        dists: list[np.ndarray] = []
        list_pages = 0
        for cluster in chosen.tolist():
            page_id = int(directory["head"][cluster])
            while page_id != NULL_PAGE:
                header, page = self._read_list_page(page_id)
                list_pages += 1
                if header.record_count:
                    rid_pages.append(page["page"])
                    rid_slots.append(page["slot"])
                    dists.append(distances(self.metric, q, page["vector"]))
                page_id = header.next_page_id
        info = {
            "probes": len(chosen),
            "lists": int(self.num_lists),
            "probed": [int(c) for c in chosen.tolist()],
            "dir_pages": self.dir.num_pages() - 1,
            "list_pages": list_pages,
            "scanned": 0,
            "hits": [],
        }
        if not dists:
            return info
        all_d = np.concatenate(dists)
        all_p = np.concatenate(rid_pages)
        all_s = np.concatenate(rid_slots)
        info["scanned"] = int(len(all_d))
        if k is not None and k < len(all_d):
            part = np.argpartition(all_d, k - 1)[:k]
            order = part[np.argsort(all_d[part], kind="stable")]
        else:
            order = np.argsort(all_d, kind="stable")
        info["hits"] = [(RID(int(all_p[i]), int(all_s[i])), float(all_d[i])) for i in order.tolist()]
        return info

    # ------------------------------------------------------------------ inspección
    def stats(self) -> dict[str, Any]:
        return {
            "dims": self.dims,
            "metric": self.metric,
            "lists": self.num_lists,
            "trained": self.trained,
            "vectors": self.entry_count,
            "list_capacity": self.list_capacity,
            "dir_pages": self.dir.num_pages(),
            "list_pages": self.lists.num_pages(),
        }

    def list_sizes(self) -> list[int]:
        return [int(c) for c in self.read_directory()["count"]]
