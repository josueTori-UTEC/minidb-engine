"""Árbol R (R-Tree) en disco sobre páginas de tamaño fijo.

Archivo ``<tabla>_<col>.rtr``:

* Página 0 (``FILE_META``): ``root_page_id``, ``height``, ``dims``, tipo de
  coordenada (``d`` = float64, ``f`` = float32), capacidades y nº de entradas.
* Nodo interno (``RTREE_INTERNAL``): ``record_count`` entradas
  ``<MBR, child_page_id>``; el MBR recubre todo el subárbol hijo.
* Nodo hoja (``RTREE_LEAF``): ``record_count`` entradas ``<MBR, RID>`` que
  apuntan al registro en la tabla de datos.

Un MBR se guarda como ``D`` mínimos seguidos de ``D`` máximos
(``<x_min, y_min, x_max, y_max>`` con ``D = 2``). Tamaño de entrada:
``2·D·c + 6`` bytes en hojas y ``2·D·c + 4`` en internos (``c`` = 8 u 4 bytes),
de modo que con ``B = 4096`` y puntos 2D en float64 entran 107 entradas por hoja
y 113 por nodo interno. Las capacidades siempre se calculan desde ``B``.

Operaciones:

* **Inserción**: descenso eligiendo la rama que necesita la menor ampliación de
  área (empates: la de menor área). Si la página desborda se aplica la división
  cuadrática de Guttman (``PickSeeds`` + ``PickNext``) con llenado mínimo del
  40 %, y el ajuste de cajas se propaga hacia la raíz (sin punteros al padre: el
  camino se guarda en una pila).
* **Ventana / radio**: se podan las ramas cuyo MBR no interseca la región
  (``MBR ∩ W = ∅`` o ``MINDIST(Q, MBR) > r``).
* **k-NN**: recorrido *best-first* con una cola de prioridad ordenada por
  ``MINDIST(Q, MBR)``; como ``MINDIST`` es cota inferior de la distancia a
  cualquier objeto del subárbol, los resultados salen en orden exacto de
  distancia y se deja de leer en cuanto se entregan ``k``.
* **Carga masiva** (``bulk_load``): empaquetado STR (*Sort-Tile-Recursive*),
  una escritura por página; se usa en ``CREATE INDEX`` sobre una tabla con datos.
* **Borrado**: quita la entrada de la hoja sin condensar el árbol (los MBR de
  los ancestros siguen siendo cotas válidas).

Las distancias admiten un vector de pesos por dimensión: para coordenadas
geográficas se usa la aproximación equirectangular (km por grado en cada eje),
con la que ``MINDIST`` sigue siendo una cota inferior válida.

La aritmética de cajas usa ``numpy`` sobre los bytes de la página; todo acceso a
disco pasa por ``DiskManager`` (una página por transferencia).
"""

from __future__ import annotations

import heapq
import itertools
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

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

RTREE_META = MetaCodec(
    FileKind.RTREE,
    [
        ("root_page_id", "i"),
        ("height", "H"),
        ("dims", "H"),
        ("coord_code", "B"),
        ("leaf_capacity", "H"),
        ("internal_capacity", "H"),
        ("entry_count", "Q"),
    ],
)

_COORD = {"d": (0, "<f8"), "f": (1, "<f4")}
_COORD_BY_CODE = {0: "d", 1: "f"}
MIN_FILL = 0.4
MAX_TRACE = 400


def rtree_capacities(page_size: int, dims: int, coord: str = "d") -> tuple[int, int]:
    """(entradas por hoja, entradas por nodo interno) para una página de ``page_size``."""
    c = np.dtype(_COORD[coord][1]).itemsize
    usable = page_size - PAGE_HEADER_SIZE
    return usable // (2 * dims * c + 6), usable // (2 * dims * c + 4)


@dataclass
class Node:
    page_id: int
    leaf: bool
    n: int
    lo: np.ndarray  # (cap + 1, D) float64
    hi: np.ndarray
    ptr: np.ndarray  # (cap + 1,) int32: child_page_id o page_id del RID
    slot: np.ndarray  # (cap + 1,) uint16: slot del RID (solo hojas)

    def mbr(self) -> tuple[np.ndarray, np.ndarray]:
        return self.lo[: self.n].min(axis=0), self.hi[: self.n].max(axis=0)


@dataclass
class SearchTrace:
    """Nodos visitados por una búsqueda (para dibujar los MBR inspeccionados)."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    visited: int = 0
    leaves: int = 0

    def add(self, node: Node, level: int) -> None:
        self.visited += 1
        if node.leaf:
            self.leaves += 1
        if len(self.nodes) < MAX_TRACE and node.n:
            lo, hi = node.mbr()
            self.nodes.append(
                {"page_id": node.page_id, "level": level, "leaf": node.leaf,
                 "mbr": [float(x) for x in lo] + [float(x) for x in hi]}
            )


def _area(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    return np.prod(hi - lo, axis=-1)


class RTree:
    def __init__(self, dm: DiskManager, meta: dict[str, Any]) -> None:
        self.dm = dm
        self.root_page_id: int = int(meta["root_page_id"])
        self.height: int = int(meta["height"])
        self.dims: int = int(meta["dims"])
        self.coord: str = _COORD_BY_CODE[int(meta["coord_code"])]
        self.leaf_capacity: int = int(meta["leaf_capacity"])
        self.internal_capacity: int = int(meta["internal_capacity"])
        self.entry_count: int = int(meta["entry_count"])
        expected = rtree_capacities(dm.page_size, self.dims, self.coord)
        if expected != (self.leaf_capacity, self.internal_capacity):
            raise PageFormatError(f"{dm.path.name}: capacidades inconsistentes con page_size={dm.page_size}")
        ctype = _COORD[self.coord][1]
        d = self.dims
        self._leaf_dtype = np.dtype(
            {"names": ["lo", "hi", "page", "slot"],
             "formats": [(ctype, (d,)), (ctype, (d,)), "<i4", "<u2"]}
        )
        self._internal_dtype = np.dtype(
            {"names": ["lo", "hi", "page"], "formats": [(ctype, (d,)), (ctype, (d,)), "<i4"]}
        )
        self._dirty = False
        self._stats_dirty = False

    # ------------------------------------------------------------------ apertura
    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        dims: int,
        page_size: int,
        counter: DiskCounter,
        *,
        coord: str = "d",
    ) -> RTree:
        if coord not in _COORD:
            raise ValueError("coord debe ser 'd' (float64) o 'f' (float32)")
        if dims < 1:
            raise ValueError("dims debe ser >= 1")
        leaf_cap, internal_cap = rtree_capacities(page_size, dims, coord)
        if leaf_cap < 2 or internal_cap < 2:
            raise PageFormatError(
                f"un R-Tree de {dims} dimensiones no cabe en páginas de {page_size} bytes "
                f"(entran {leaf_cap} entradas por hoja; se necesitan al menos 2)"
            )
        dm = DiskManager(path, page_size, counter, create=True)
        meta = {
            "root_page_id": 1,
            "height": 1,
            "dims": dims,
            "coord_code": _COORD[coord][0],
            "leaf_capacity": leaf_cap,
            "internal_capacity": internal_cap,
            "entry_count": 0,
        }
        dm.allocate_page()
        tree = cls(dm, meta)
        root = tree._new_node(leaf=True)
        tree._write(root)
        dm.write_page(0, tree._meta_bytes())
        return tree

    @classmethod
    def open(cls, path: str | os.PathLike[str], page_size: int, counter: DiskCounter) -> RTree:
        dm = DiskManager(path, page_size, counter)
        _, _, meta = RTREE_META.unpack(dm.read_page(0))
        return cls(dm, meta)

    def _meta_bytes(self) -> bytes:
        return RTREE_META.pack(
            self.dm.page_size,
            self.dm.num_pages(),
            {
                "root_page_id": self.root_page_id,
                "height": self.height,
                "dims": self.dims,
                "coord_code": _COORD[self.coord][0],
                "leaf_capacity": self.leaf_capacity,
                "internal_capacity": self.internal_capacity,
                "entry_count": self.entry_count,
            },
        )

    def flush(self) -> None:
        """Fin de sentencia: escribe la página 0 solo si cambió la estructura (raíz o altura)."""
        if self._dirty:
            self.dm.write_page(0, self._meta_bytes())
            self._dirty = self._stats_dirty = False

    def sync(self) -> None:
        if self._dirty or self._stats_dirty:
            self.dm.write_page(0, self._meta_bytes())
            self._dirty = self._stats_dirty = False
        self.dm.flush()

    def close(self) -> None:
        if not self.dm.closed:
            self.sync()
            self.dm.close()

    @property
    def page_count(self) -> int:
        return self.dm.num_pages()

    # ------------------------------------------------------------------ nodos
    def _new_node(self, leaf: bool) -> Node:
        cap = self.leaf_capacity if leaf else self.internal_capacity
        d = self.dims
        return Node(
            self.dm.allocate_page(), leaf, 0,
            np.zeros((cap + 1, d)), np.zeros((cap + 1, d)),
            np.zeros(cap + 1, dtype=np.int32), np.zeros(cap + 1, dtype=np.uint16),
        )

    def _read(self, page_id: int) -> Node:
        data = self.dm.read_page(page_id)
        header = PageHeader.unpack(data)
        expect_page_type(header, PageType.RTREE_LEAF, PageType.RTREE_INTERNAL)
        leaf = header.page_type == PageType.RTREE_LEAF
        n = header.record_count
        cap = self.leaf_capacity if leaf else self.internal_capacity
        d = self.dims
        arr = np.frombuffer(data, dtype=self._leaf_dtype if leaf else self._internal_dtype, count=n,
                            offset=PAGE_HEADER_SIZE)
        lo = np.zeros((cap + 1, d))
        hi = np.zeros((cap + 1, d))
        ptr = np.zeros(cap + 1, dtype=np.int32)
        slot = np.zeros(cap + 1, dtype=np.uint16)
        if n:
            lo[:n] = arr["lo"]
            hi[:n] = arr["hi"]
            ptr[:n] = arr["page"]
            if leaf:
                slot[:n] = arr["slot"]
        return Node(page_id, leaf, n, lo, hi, ptr, slot)

    def _write(self, node: Node) -> None:
        n = node.n
        arr = np.zeros(n, dtype=self._leaf_dtype if node.leaf else self._internal_dtype)
        if n:
            arr["lo"] = node.lo[:n]
            arr["hi"] = node.hi[:n]
            arr["page"] = node.ptr[:n]
            if node.leaf:
                arr["slot"] = node.slot[:n]
        buf = bytearray(self.dm.page_size)
        PageHeader(
            page_id=node.page_id,
            page_type=PageType.RTREE_LEAF if node.leaf else PageType.RTREE_INTERNAL,
            record_count=n,
            free_space_offset=PAGE_HEADER_SIZE + arr.nbytes,
        ).pack_into(buf)
        buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + arr.nbytes] = arr.tobytes()
        self.dm.write_page(node.page_id, bytes(buf))

    def _round(self, values: Sequence[float]) -> np.ndarray:
        """Lleva las coordenadas a la precisión con la que se guardan en disco."""
        arr = np.asarray(values, dtype=np.float64)
        if arr.shape != (self.dims,):
            raise ValueError(f"se esperaban {self.dims} coordenadas y se recibieron {arr.size}")
        return arr.astype(_COORD[self.coord][1]).astype(np.float64) if self.coord == "f" else arr

    # ------------------------------------------------------------------ inserción
    def insert(self, lo: Sequence[float], hi: Sequence[float] | None, rid: RID) -> None:
        """Inserta ``<MBR, RID>``; para un punto ``hi`` es ``None`` (MBR degenerado)."""
        lo_a = self._round(lo)
        hi_a = lo_a if hi is None else self._round(hi)
        path: list[tuple[Node, int]] = []
        node = self._read(self.root_page_id)
        while not node.leaf:
            i = self._choose_subtree(node, lo_a, hi_a)
            path.append((node, i))
            node = self._read(int(node.ptr[i]))
        n = node.n
        node.lo[n], node.hi[n], node.ptr[n], node.slot[n] = lo_a, hi_a, rid.page_id, rid.slot
        node.n = n + 1
        self.entry_count += 1
        self._stats_dirty = True
        self._propagate(node, path, lo_a, hi_a)

    def _choose_subtree(self, node: Node, lo: np.ndarray, hi: np.ndarray) -> int:
        n = node.n
        nlo, nhi = node.lo[:n], node.hi[:n]
        area = _area(nlo, nhi)
        enlarged = _area(np.minimum(nlo, lo), np.maximum(nhi, hi)) - area
        best = np.flatnonzero(enlarged == enlarged.min())
        if len(best) == 1:
            return int(best[0])
        # empate (habitual con cajas degeneradas o en alta dimensión): menor área, luego menor margen
        sub = best[area[best] == area[best].min()]
        if len(sub) == 1:
            return int(sub[0])
        margin = (np.maximum(nhi[sub], hi) - np.minimum(nlo[sub], lo)).sum(axis=1) - (nhi[sub] - nlo[sub]).sum(axis=1)
        return int(sub[int(np.argmin(margin))])

    def _propagate(self, node: Node, path: list[tuple[Node, int]], lo: np.ndarray, hi: np.ndarray) -> None:
        """Escribe ``node`` (dividiéndolo si desborda) y ajusta los MBR hacia la raíz."""
        cap = self.leaf_capacity if node.leaf else self.internal_capacity
        sibling: Node | None = None
        if node.n > cap:
            sibling = self._split(node)
            self._write(sibling)
        self._write(node)
        while path:
            parent, i = path.pop()
            changed = False
            if sibling is None:
                # solo hay que agrandar la entrada del padre si la caja nueva no cabía
                if np.any(lo < parent.lo[i]) or np.any(hi > parent.hi[i]):
                    parent.lo[i] = np.minimum(parent.lo[i], lo)
                    parent.hi[i] = np.maximum(parent.hi[i], hi)
                    changed = True
            else:
                parent.lo[i], parent.hi[i] = node.mbr()
                n = parent.n
                parent.lo[n], parent.hi[n] = sibling.mbr()
                parent.ptr[n] = sibling.page_id
                parent.n = n + 1
                changed = True
            if not changed:
                return
            sibling = None
            if parent.n > self.internal_capacity:
                sibling = self._split(parent)
                self._write(sibling)
            self._write(parent)
            node = parent
        if sibling is not None:
            root = self._new_node(leaf=False)
            root.lo[0], root.hi[0] = node.mbr()
            root.ptr[0] = node.page_id
            root.lo[1], root.hi[1] = sibling.mbr()
            root.ptr[1] = sibling.page_id
            root.n = 2
            self._write(root)
            self.root_page_id = root.page_id
            self.height += 1
            self._dirty = True

    def _split(self, node: Node) -> Node:
        """División cuadrática de Guttman; deja un grupo en ``node`` y devuelve el otro."""
        n = node.n
        lo, hi = node.lo[:n].copy(), node.hi[:n].copy()
        ptr, slot = node.ptr[:n].copy(), node.slot[:n].copy()
        area = _area(lo, hi)
        # PickSeeds: el par que desperdicia más área al juntarse
        waste = _area(np.minimum(lo[:, None, :], lo[None, :, :]), np.maximum(hi[:, None, :], hi[None, :, :]))
        waste = waste - area[:, None] - area[None, :]
        np.fill_diagonal(waste, -np.inf)
        s1, s2 = divmod(int(np.argmax(waste)), n)
        if s1 == s2:  # todas las entradas idénticas
            s1, s2 = 0, n - 1
        groups: tuple[list[int], list[int]] = ([s1], [s2])
        glo = [lo[s1].copy(), lo[s2].copy()]
        ghi = [hi[s1].copy(), hi[s2].copy()]
        remaining = np.ones(n, dtype=bool)
        remaining[[s1, s2]] = False
        min_fill = max(1, math.ceil(MIN_FILL * (n - 1)))
        left = n - 2
        while left:
            # si a un grupo le faltan justo las que quedan para llegar al mínimo, se las lleva todas
            for g in (0, 1):
                if len(groups[g]) + left == min_fill:
                    rest = np.flatnonzero(remaining)
                    groups[g].extend(int(i) for i in rest)
                    glo[g] = np.minimum(glo[g], lo[rest].min(axis=0))
                    ghi[g] = np.maximum(ghi[g], hi[rest].max(axis=0))
                    left = 0
                    break
            if not left:
                break
            idx = np.flatnonzero(remaining)
            a0, a1 = _area(glo[0], ghi[0]), _area(glo[1], ghi[1])
            d0 = _area(np.minimum(lo[idx], glo[0]), np.maximum(hi[idx], ghi[0])) - a0
            d1 = _area(np.minimum(lo[idx], glo[1]), np.maximum(hi[idx], ghi[1])) - a1
            # PickNext: la entrada con mayor preferencia por uno de los grupos
            k = int(np.argmax(np.abs(d0 - d1)))
            e = int(idx[k])
            if d0[k] < d1[k]:
                g = 0
            elif d1[k] < d0[k]:
                g = 1
            elif a0 != a1:
                g = 0 if a0 < a1 else 1
            else:
                g = 0 if len(groups[0]) <= len(groups[1]) else 1
            groups[g].append(e)
            glo[g] = np.minimum(glo[g], lo[e])
            ghi[g] = np.maximum(ghi[g], hi[e])
            remaining[e] = False
            left -= 1
        sibling = self._new_node(node.leaf)
        for target, members in ((node, groups[0]), (sibling, groups[1])):
            m = len(members)
            target.lo[:m], target.hi[:m] = lo[members], hi[members]
            target.ptr[:m], target.slot[:m] = ptr[members], slot[members]
            target.n = m
        return sibling

    # ------------------------------------------------------------------ carga masiva (STR)
    def bulk_load(self, items: Iterator[tuple[Sequence[float], RID]], fill_factor: float = 1.0) -> None:
        """Empaquetado *Sort-Tile-Recursive* de puntos sobre un árbol vacío.

        Ordena los puntos por la primera coordenada, los corta en franjas y dentro
        de cada franja los ordena por la siguiente; cada grupo de ``capacidad``
        puntos es una hoja. Los niveles superiores se empaquetan igual sobre los
        centros de las cajas. Cada página se escribe una sola vez.
        """
        if self.entry_count or self.height != 1:
            raise RuntimeError("bulk_load solo se puede usar sobre un árbol vacío")
        points: list[Sequence[float]] = []
        rids: list[RID] = []
        for point, rid in items:
            points.append(point)
            rids.append(rid)
        n = len(points)
        if n == 0:
            return
        ctype = _COORD[self.coord][1]
        lo = np.asarray(points, dtype=np.float64).reshape(n, self.dims)
        if self.coord == "f":
            lo = lo.astype(ctype).astype(np.float64)
        hi = lo
        ptr = np.fromiter((r.page_id for r in rids), dtype=np.int32, count=n)
        slot = np.fromiter((r.slot for r in rids), dtype=np.uint16, count=n)
        leaf = True
        height = 1
        first_page = self.root_page_id  # la raíz vacía se reutiliza como primera hoja
        while True:
            cap = self.leaf_capacity if leaf else self.internal_capacity
            per_node = max(2, int(cap * fill_factor))
            order = self._str_order(lo, hi, per_node)
            lo, hi, ptr, slot = lo[order], hi[order], ptr[order], slot[order]
            count = len(lo)
            node_count = math.ceil(count / per_node)
            # reparto parejo: ningún nodo queda por debajo de la mitad salvo que haya uno solo
            base, extra = divmod(count, node_count)
            new_lo = np.zeros((node_count, self.dims))
            new_hi = np.zeros((node_count, self.dims))
            new_ptr = np.zeros(node_count, dtype=np.int32)
            start = 0
            for j in range(node_count):
                size = base + (1 if j < extra else 0)
                if first_page is not None:
                    page_id, first_page = first_page, None
                else:
                    page_id = self.dm.allocate_page()
                node = Node(page_id, leaf, size, lo[start : start + size], hi[start : start + size],
                            ptr[start : start + size], slot[start : start + size])
                self._write(node)
                new_lo[j], new_hi[j] = node.mbr()
                new_ptr[j] = page_id
                start += size
            if node_count == 1:
                self.root_page_id = int(new_ptr[0])
                break
            lo, hi, ptr = new_lo, new_hi, new_ptr
            slot = np.zeros(node_count, dtype=np.uint16)
            leaf = False
            height += 1
        self.height = height
        self.entry_count = n
        self._dirty = True

    def _str_order(self, lo: np.ndarray, hi: np.ndarray, per_node: int) -> np.ndarray:
        centers = (lo + hi) / 2
        n = len(centers)
        order = np.arange(n)

        def tile(idx: np.ndarray, dim: int) -> np.ndarray:
            idx = idx[np.argsort(centers[idx, dim], kind="stable")]
            if dim == self.dims - 1 or len(idx) <= per_node:
                return idx
            pages = math.ceil(len(idx) / per_node)
            slices = math.ceil(pages ** (1 / (self.dims - dim)))
            slice_size = math.ceil(len(idx) / slices)
            parts = [tile(idx[s : s + slice_size], dim + 1) for s in range(0, len(idx), slice_size)]
            return np.concatenate(parts)

        return tile(order, 0)

    # ------------------------------------------------------------------ búsquedas
    def search_window(
        self, lo: Sequence[float], hi: Sequence[float], trace: SearchTrace | None = None
    ) -> Iterator[tuple[RID, np.ndarray]]:
        """Entradas cuyo MBR interseca la ventana ``[lo, hi]`` (bordes incluidos)."""
        qlo = np.asarray(lo, dtype=np.float64)
        qhi = np.asarray(hi, dtype=np.float64)
        stack = [(self.root_page_id, self.height)]
        while stack:
            page_id, level = stack.pop()
            node = self._read(page_id)
            if trace is not None:
                trace.add(node, level)
            n = node.n
            if not n:
                continue
            hit = np.flatnonzero(np.all(node.lo[:n] <= qhi, axis=1) & np.all(node.hi[:n] >= qlo, axis=1))
            if node.leaf:
                for i in hit:
                    yield RID(int(node.ptr[i]), int(node.slot[i])), node.lo[i]
            else:
                stack.extend((int(node.ptr[i]), level - 1) for i in hit[::-1])

    def _mindist(self, q: np.ndarray, lo: np.ndarray, hi: np.ndarray, weights: np.ndarray | None) -> np.ndarray:
        delta = np.maximum(lo - q, 0.0) + np.maximum(q - hi, 0.0)
        if weights is not None:
            delta = delta * weights
        return np.sqrt(np.einsum("ij,ij->i", delta, delta))

    def search_radius(
        self,
        center: Sequence[float],
        radius: float,
        weights: Sequence[float] | None = None,
        trace: SearchTrace | None = None,
    ) -> Iterator[tuple[RID, np.ndarray, float]]:
        """Entradas con ``MINDIST(center, MBR) <= radius`` (distancia euclidiana ponderada)."""
        q = np.asarray(center, dtype=np.float64)
        w = None if weights is None else np.asarray(weights, dtype=np.float64)
        stack = [(self.root_page_id, self.height)]
        while stack:
            page_id, level = stack.pop()
            node = self._read(page_id)
            if trace is not None:
                trace.add(node, level)
            n = node.n
            if not n:
                continue
            dist = self._mindist(q, node.lo[:n], node.hi[:n], w)
            hit = np.flatnonzero(dist <= radius)
            if node.leaf:
                for i in hit:
                    yield RID(int(node.ptr[i]), int(node.slot[i])), node.lo[i], float(dist[i])
            else:
                stack.extend((int(node.ptr[i]), level - 1) for i in hit[::-1])

    def nearest(
        self,
        point: Sequence[float],
        weights: Sequence[float] | None = None,
        trace: SearchTrace | None = None,
    ) -> Iterator[tuple[RID, np.ndarray, float]]:
        """Entradas en orden creciente de distancia a ``point`` (*best-first*).

        Es un generador: el llamador toma las ``k`` primeras (o sigue pidiendo si
        aplica un filtro) y solo se leen los nodos necesarios para garantizarlas.
        """
        q = np.asarray(point, dtype=np.float64)
        w = None if weights is None else np.asarray(weights, dtype=np.float64)
        tie = itertools.count()
        # (distancia, desempate, es_nodo, page_id/nivel o (rid, punto))
        heap: list[tuple[float, int, int, Any]] = [(0.0, next(tie), 1, (self.root_page_id, self.height))]
        while heap:
            dist, _, is_node, payload = heapq.heappop(heap)
            if not is_node:
                rid, lo = payload
                yield rid, lo, dist
                continue
            page_id, level = payload
            node = self._read(page_id)
            if trace is not None:
                trace.add(node, level)
            n = node.n
            if not n:
                continue
            dists = self._mindist(q, node.lo[:n], node.hi[:n], w)
            if node.leaf:
                for i in range(n):
                    heapq.heappush(
                        heap, (float(dists[i]), next(tie), 0, (RID(int(node.ptr[i]), int(node.slot[i])), node.lo[i]))
                    )
            else:
                for i in range(n):
                    heapq.heappush(heap, (float(dists[i]), next(tie), 1, (int(node.ptr[i]), level - 1)))

    def knn(
        self, point: Sequence[float], k: int, weights: Sequence[float] | None = None,
        trace: SearchTrace | None = None,
    ) -> list[tuple[RID, np.ndarray, float]]:
        return list(itertools.islice(self.nearest(point, weights, trace), k))

    # ------------------------------------------------------------------ borrado
    def delete(self, lo: Sequence[float], hi: Sequence[float] | None, rid: RID) -> bool:
        """Quita la entrada ``<MBR, rid>`` de su hoja (sin condensar el árbol)."""
        lo_a = self._round(lo)
        hi_a = lo_a if hi is None else self._round(hi)
        stack = [self.root_page_id]
        while stack:
            node = self._read(stack.pop())
            n = node.n
            if not n:
                continue
            hit = np.flatnonzero(np.all(node.lo[:n] <= hi_a, axis=1) & np.all(node.hi[:n] >= lo_a, axis=1))
            if not node.leaf:
                stack.extend(int(node.ptr[i]) for i in hit)
                continue
            for i in hit:
                if int(node.ptr[i]) == rid.page_id and int(node.slot[i]) == rid.slot:
                    last = n - 1
                    node.lo[i], node.hi[i] = node.lo[last], node.hi[last]
                    node.ptr[i], node.slot[i] = node.ptr[last], node.slot[last]
                    node.n = last
                    self._write(node)
                    self.entry_count -= 1
                    self._stats_dirty = True
                    return True
        return False

    # ------------------------------------------------------------------ inspección
    def iter_all(self) -> Iterator[tuple[RID, np.ndarray]]:
        stack = [self.root_page_id]
        while stack:
            node = self._read(stack.pop())
            if node.leaf:
                for i in range(node.n):
                    yield RID(int(node.ptr[i]), int(node.slot[i])), node.lo[i].copy()
            else:
                stack.extend(int(node.ptr[i]) for i in range(node.n))

    def check_invariants(self) -> dict[str, int]:
        """Verifica contención de MBR, profundidad uniforme y nº de entradas (para tests)."""
        stats = {"nodes": 0, "leaves": 0, "entries": 0}

        def visit(page_id: int, depth: int, plo: np.ndarray | None, phi: np.ndarray | None) -> None:
            node = self._read(page_id)
            stats["nodes"] += 1
            cap = self.leaf_capacity if node.leaf else self.internal_capacity
            assert node.n <= cap, f"nodo {page_id} con {node.n} entradas (capacidad {cap})"
            if node.n and plo is not None and phi is not None:
                lo, hi = node.mbr()
                assert np.all(lo >= plo) and np.all(hi <= phi), f"el MBR del nodo {page_id} se sale del de su padre"
            if node.leaf:
                assert depth == self.height, f"hoja {page_id} a profundidad {depth} (altura {self.height})"
                stats["leaves"] += 1
                stats["entries"] += node.n
                return
            assert node.n >= 1, f"nodo interno {page_id} vacío"
            for i in range(node.n):
                visit(int(node.ptr[i]), depth + 1, node.lo[i].copy(), node.hi[i].copy())

        visit(self.root_page_id, 1, None, None)
        assert stats["entries"] == self.entry_count, (stats["entries"], self.entry_count)
        return stats

    def describe_node(self, page_id: int, limit: int = 200) -> dict[str, Any]:
        """Contenido decodificado de un nodo (para ``dump_page`` y el inspector)."""
        node = self._read(page_id)
        entries = []
        for i in range(min(node.n, limit)):
            mbr = [float(x) for x in node.lo[i]] + [float(x) for x in node.hi[i]]
            if node.leaf:
                entries.append({"mbr": mbr, "rid": [int(node.ptr[i]), int(node.slot[i])]})
            else:
                entries.append({"mbr": mbr, "child": int(node.ptr[i])})
        out: dict[str, Any] = {
            "kind": "hoja" if node.leaf else "interno",
            "entries_count": node.n,
            "capacity": self.leaf_capacity if node.leaf else self.internal_capacity,
            "entries": entries,
        }
        if node.n:
            lo, hi = node.mbr()
            out["mbr"] = [float(x) for x in lo] + [float(x) for x in hi]
        return out


# ---------------------------------------------------------------------- geografía
EARTH_RADIUS_KM = 6371.0088
KM_PER_DEG_LAT = 111.1949  # 2·π·R / 360


def geo_weights(lat: float) -> tuple[float, float]:
    """Km por grado de (longitud, latitud) alrededor de ``lat``.

    Para la longitud se usa el coseno de la latitud más alejada del ecuador que
    puede alcanzar la consulta, así la distancia ponderada nunca sobreestima la
    real y ``MINDIST`` sigue siendo cota inferior.
    """
    return KM_PER_DEG_LAT * max(0.0, math.cos(math.radians(lat))), KM_PER_DEG_LAT


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))
