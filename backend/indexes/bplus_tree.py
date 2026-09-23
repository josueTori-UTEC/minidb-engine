"""Árbol B+ multinivel en disco (índice no agrupado: hojas con ``<clave, RID>``).

Archivo ``<tabla>_<col>.bpt``:

* Página 0 (``FILE_META``): ``root_page_id``, ``height``, tipo y tamaño de clave,
  capacidades de hoja e interno, nº de entradas y de hojas.
* Nodo interno (``BTREE_INTERNAL``): ``<P0, K1, P1, ..., Km, Pm>`` empaquetado como
  ``'<i' + (kfmt + 'i') * m``. ``record_count`` = m.
  ``m_max = (B - 24 - 4) // (key_size + 4)`` -> fan-out ``m_max + 1``.
* Nodo hoja (``BTREE_LEAF``): pares ordenados ``<key, RID>`` empaquetados como
  ``(kfmt + 'iH') * n``, con ``cap = (B - 24) // (key_size + 6)``.
  ``next_page_id``/``prev_page_id`` = ``next_leaf``/``prev_leaf``.

Con clave INT: hojas 100 / 202 / 407 / 816 e internos 124 / 252 / 508 / 1020 claves
para B = 1024 / 2048 / 4096 / 8192. El orden siempre se calcula desde B.

Operaciones:

* Inserción: descenso guardando el camino en una pila (no hay punteros al
  padre), split al 50 % con propagación hacia arriba y nueva raíz si la raíz
  desborda.
* Búsqueda puntual: h lecturas hasta la hoja (+ hojas vecinas si hay duplicados).
* Rango: descenso a la primera hoja con ``key >= lo`` y recorrido por
  ``next_leaf``.
* Duplicados permitidos: se busca la hoja más a la izquierda con ``key >= k``
  (``bisect_left`` en los internos) y se avanza a la derecha; la inserción baja
  por ``bisect_right`` (el nuevo duplicado queda después de los existentes).
* Borrado: se quita la entrada de la hoja, sin merge ni redistribución (las hojas
  pueden quedar sub-llenas; las cotas de los separadores siguen siendo válidas).
* Índice único (PK): la bajada usa ``bisect_right`` también al buscar, así una
  búsqueda puntual cuesta exactamente h lecturas, y el insert rechaza claves
  repetidas sin lecturas adicionales.

Las claves CHAR se comparan como bytes UTF-8 rellenados con ``\\x00`` (mismo orden
que los strings por punto de código).
"""

from __future__ import annotations

import os
import struct
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any, Iterator

from backend.files.common import DuplicateKeyError
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
from backend.storage.record import Column, ColumnType, KeyCodec, encode_str
from backend.storage.rid import RID, RID_SIZE

BPT_META = MetaCodec(
    FileKind.BPLUS,
    [
        ("root_page_id", "i"),
        ("height", "H"),
        ("key_type", "B"),
        ("key_size", "H"),
        ("leaf_capacity", "H"),
        ("internal_capacity", "H"),
        ("entry_count", "Q"),
        ("leaf_pages", "I"),
        ("unique", "B"),
        ("has_bounds", "B"),  # min/max de claves numéricas (estadística para el planner)
        ("min_key", "d"),
        ("max_key", "d"),
    ],
)
POINTER_SIZE = 4


def leaf_capacity(page_size: int, key_size: int) -> int:
    return (page_size - PAGE_HEADER_SIZE) // (key_size + RID_SIZE)


def internal_capacity(page_size: int, key_size: int) -> int:
    return (page_size - PAGE_HEADER_SIZE - POINTER_SIZE) // (key_size + POINTER_SIZE)


@dataclass(slots=True)
class LeafNode:
    page_id: int
    keys: list[Any] = field(default_factory=list)
    pids: list[int] = field(default_factory=list)
    slots: list[int] = field(default_factory=list)
    next_leaf: int = NULL_PAGE
    prev_leaf: int = NULL_PAGE


@dataclass(slots=True)
class InternalNode:
    page_id: int
    keys: list[Any] = field(default_factory=list)
    children: list[int] = field(default_factory=list)


class BPlusTree:
    def __init__(self, dm: DiskManager, column: Column, meta: dict[str, Any]) -> None:
        self.dm = dm
        self.column = column
        self.codec = KeyCodec(column)
        self.key_size = self.codec.size
        self._kfmt = column.fmt
        self._is_char = column.type == ColumnType.CHAR
        self.leaf_capacity = leaf_capacity(dm.page_size, self.key_size)
        self.internal_capacity = internal_capacity(dm.page_size, self.key_size)
        if self.leaf_capacity < 3 or self.internal_capacity < 3:
            raise PageFormatError(f"page_size {dm.page_size} demasiado pequeño para claves de {self.key_size} bytes")
        if int(meta["key_size"]) != self.key_size or int(meta["key_type"]) != self.codec.type_code:
            raise PageFormatError(f"{dm.path.name}: el tipo de clave no coincide con el archivo")
        self.root: int = int(meta["root_page_id"])
        self.height: int = int(meta["height"])
        self.entry_count: int = int(meta["entry_count"])
        self.leaf_pages: int = int(meta["leaf_pages"])
        self.unique = bool(meta["unique"])
        self.min_key: float | None = float(meta["min_key"]) if meta["has_bounds"] else None
        self.max_key: float | None = float(meta["max_key"]) if meta["has_bounds"] else None
        self._dirty = False
        self._stats_dirty = False
        self._leaf_structs: dict[int, struct.Struct] = {}
        self._internal_structs: dict[int, struct.Struct] = {}

    # ------------------------------------------------------------------ apertura
    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        column: Column,
        page_size: int,
        counter: DiskCounter,
        *,
        unique: bool = False,
    ) -> BPlusTree:
        """``unique=True`` (índice sobre la PK) rechaza claves repetidas en la misma
        bajada del insert y permite búsquedas puntuales de exactamente h lecturas."""
        dm = DiskManager(path, page_size, counter, create=True)
        codec = KeyCodec(column)
        meta = {
            "root_page_id": NULL_PAGE,
            "height": 0,
            "key_type": codec.type_code,
            "key_size": codec.size,
            "leaf_capacity": leaf_capacity(page_size, codec.size),
            "internal_capacity": internal_capacity(page_size, codec.size),
            "entry_count": 0,
            "leaf_pages": 0,
            "unique": 1 if unique else 0,
            "has_bounds": 0,
            "min_key": 0.0,
            "max_key": 0.0,
        }
        dm.write_page(dm.allocate_page(), BPT_META.pack(page_size, 1, meta))
        return cls(dm, column, meta)

    @classmethod
    def open(cls, path: str | os.PathLike[str], column: Column, page_size: int, counter: DiskCounter) -> BPlusTree:
        dm = DiskManager(path, page_size, counter)
        stored_ps, _, meta = BPT_META.unpack(dm.read_page(0))
        if stored_ps != page_size:
            dm.close()
            raise PageFormatError(f"{dm.path.name}: page_size {stored_ps} != {page_size}")
        return cls(dm, column, meta)

    def _meta_bytes(self) -> bytes:
        return BPT_META.pack(
            self.dm.page_size,
            self.dm.num_pages(),
            {
                "root_page_id": self.root,
                "height": self.height,
                "key_type": self.codec.type_code,
                "key_size": self.key_size,
                "leaf_capacity": self.leaf_capacity,
                "internal_capacity": self.internal_capacity,
                "entry_count": self.entry_count,
                "leaf_pages": self.leaf_pages,
                "unique": 1 if self.unique else 0,
                "has_bounds": 0 if self.min_key is None else 1,
                "min_key": 0.0 if self.min_key is None else self.min_key,
                "max_key": 0.0 if self.max_key is None else self.max_key,
            },
        )

    def flush(self) -> None:
        """Escribe la página 0 si cambió la raíz o la altura (fin de sentencia)."""
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

    # ------------------------------------------------------------------ estadísticas
    @property
    def page_count(self) -> int:
        return self.dm.num_pages() - 1

    @property
    def fan_out(self) -> int:
        return self.internal_capacity + 1

    # ------------------------------------------------------------------ claves
    def comparable(self, value: Any) -> Any:
        """Dominio de comparación de las claves (CHAR -> bytes rellenados)."""
        if self._is_char:
            return encode_str(value, self.key_size).ljust(self.key_size, b"\x00")
        return value

    def display(self, key: Any) -> Any:
        return self.codec.decode_raw(key) if self._is_char else key

    # ------------------------------------------------------------------ (de)serialización
    def _leaf_struct(self, n: int) -> struct.Struct:
        s = self._leaf_structs.get(n)
        if s is None:
            s = self._leaf_structs[n] = struct.Struct("<" + (self._kfmt + "iH") * n)
        return s

    def _internal_struct(self, m: int) -> struct.Struct:
        s = self._internal_structs.get(m)
        if s is None:
            s = self._internal_structs[m] = struct.Struct("<i" + (self._kfmt + "i") * m)
        return s

    def _read_node(self, page_id: int) -> LeafNode | InternalNode:
        data = self.dm.read_page(page_id)
        header = PageHeader.unpack(data)
        n = header.record_count
        if header.page_type == PageType.BTREE_LEAF:
            vals = self._leaf_struct(n).unpack_from(data, PAGE_HEADER_SIZE)
            return LeafNode(
                page_id, list(vals[0::3]), list(vals[1::3]), list(vals[2::3]), header.next_page_id, header.prev_page_id
            )
        expect_page_type(header, PageType.BTREE_INTERNAL)
        vals = self._internal_struct(n).unpack_from(data, PAGE_HEADER_SIZE)
        return InternalNode(page_id, list(vals[1::2]), list(vals[0::2]))

    def _write_leaf(self, leaf: LeafNode) -> None:
        n = len(leaf.keys)
        buf = bytearray(self.dm.page_size)
        body = self._leaf_struct(n)
        PageHeader(
            page_id=leaf.page_id,
            page_type=PageType.BTREE_LEAF,
            record_count=n,
            free_space_offset=PAGE_HEADER_SIZE + body.size,
            next_page_id=leaf.next_leaf,
            prev_page_id=leaf.prev_leaf,
        ).pack_into(buf)
        flat = [x for triple in zip(leaf.keys, leaf.pids, leaf.slots) for x in triple]
        body.pack_into(buf, PAGE_HEADER_SIZE, *flat)
        self.dm.write_page(leaf.page_id, buf)

    def _write_internal(self, node: InternalNode) -> None:
        m = len(node.keys)
        buf = bytearray(self.dm.page_size)
        body = self._internal_struct(m)
        PageHeader(
            page_id=node.page_id,
            page_type=PageType.BTREE_INTERNAL,
            record_count=m,
            free_space_offset=PAGE_HEADER_SIZE + body.size,
        ).pack_into(buf)
        flat: list[Any] = [node.children[0]]
        for k, child in zip(node.keys, node.children[1:]):
            flat.append(k)
            flat.append(child)
        body.pack_into(buf, PAGE_HEADER_SIZE, *flat)
        self.dm.write_page(node.page_id, buf)

    def _raw_key(self, value: Any) -> Any:
        """Clave tal como se empaqueta (CHAR -> bytes)."""
        if self._is_char:
            return encode_str(value, self.key_size).ljust(self.key_size, b"\x00")
        if self.column.type == ColumnType.FLOAT:
            return float(value)
        return value

    # ------------------------------------------------------------------ inserción
    def insert(self, value: Any, rid: RID) -> None:
        """Inserta ``(value, rid)``. En un índice único lanza ``DuplicateKeyError`` si
        la clave ya existe (se detecta en la misma hoja, sin lecturas extra)."""
        key = self._raw_key(value)
        if not self._is_char:
            self._track_bounds(key)
        if self.root == NULL_PAGE:
            self.entry_count += 1
            self._stats_dirty = True
            leaf = LeafNode(self.dm.allocate_page(), [key], [rid.page_id], [rid.slot])
            self._write_leaf(leaf)
            self.root, self.height, self.leaf_pages = leaf.page_id, 1, 1
            self._dirty = True
            return

        path: list[tuple[InternalNode, int]] = []
        node = self._read_node(self.root)
        while isinstance(node, InternalNode):
            i = bisect_right(node.keys, key)
            path.append((node, i))
            node = self._read_node(node.children[i])
        leaf = node
        pos = bisect_right(leaf.keys, key)
        if self.unique and pos > 0 and leaf.keys[pos - 1] == key:
            raise DuplicateKeyError(f"clave duplicada en índice único: {self.display(key)}")
        self.entry_count += 1
        self._stats_dirty = True
        leaf.keys.insert(pos, key)
        leaf.pids.insert(pos, rid.page_id)
        leaf.slots.insert(pos, rid.slot)
        if len(leaf.keys) <= self.leaf_capacity:
            self._write_leaf(leaf)
            return

        # Split de la hoja al 50 %: la mitad derecha va a una hoja nueva.
        mid = len(leaf.keys) // 2
        right = LeafNode(
            self.dm.allocate_page(),
            leaf.keys[mid:],
            leaf.pids[mid:],
            leaf.slots[mid:],
            next_leaf=leaf.next_leaf,
            prev_leaf=leaf.page_id,
        )
        del leaf.keys[mid:], leaf.pids[mid:], leaf.slots[mid:]
        leaf.next_leaf = right.page_id
        if right.next_leaf != NULL_PAGE:
            neighbor = self._read_node(right.next_leaf)
            assert isinstance(neighbor, LeafNode)
            neighbor.prev_leaf = right.page_id
            self._write_leaf(neighbor)
        self._write_leaf(leaf)
        self._write_leaf(right)
        self.leaf_pages += 1
        separator, new_child = right.keys[0], right.page_id

        # Propagación hacia arriba usando la pila del descenso.
        while path:
            parent, i = path.pop()
            parent.keys.insert(i, separator)
            parent.children.insert(i + 1, new_child)
            if len(parent.keys) <= self.internal_capacity:
                self._write_internal(parent)
                return
            mid = len(parent.keys) // 2
            separator = parent.keys[mid]  # sube al padre (no se copia)
            sibling = InternalNode(self.dm.allocate_page(), parent.keys[mid + 1 :], parent.children[mid + 1 :])
            del parent.keys[mid:], parent.children[mid + 1 :]
            self._write_internal(parent)
            self._write_internal(sibling)
            new_child = sibling.page_id

        # La raíz desbordó: nueva raíz con dos hijos.
        new_root = InternalNode(self.dm.allocate_page(), [separator], [self.root, new_child])
        self._write_internal(new_root)
        self.root = new_root.page_id
        self.height += 1
        self._dirty = True

    def _track_bounds(self, key: Any) -> None:
        if self.min_key is None or key < self.min_key:
            self.min_key = float(key)
            self._stats_dirty = True
        if self.max_key is None or key > self.max_key:
            self.max_key = float(key)
            self._stats_dirty = True

    def bulk_load(self, items: Iterator[tuple[Any, RID]], fill_factor: float = 1.0) -> None:
        """Construye el árbol de abajo hacia arriba a partir de pares ORDENADOS por clave.

        Escribe cada hoja una sola vez (llenas al ``fill_factor``) y luego los niveles
        internos; en memoria solo se guarda la primera clave de cada nodo del nivel
        en construcción. Se usa al reconstruir el índice de la PK tras reorganizar un
        Sequential File (el recorrido sale ordenado).
        """
        if self.root != NULL_PAGE:
            raise PageFormatError("bulk_load requiere un árbol vacío")
        per_leaf = max(1, int(self.leaf_capacity * fill_factor))
        level: list[tuple[Any, int]] = []  # (primera clave, page_id) de cada nodo del nivel
        current: LeafNode | None = None
        last_key = None
        for value, rid in items:
            key = self._raw_key(value)
            if last_key is not None and key < last_key:
                raise PageFormatError("bulk_load recibió claves desordenadas")
            if self.unique and last_key is not None and key == last_key:
                raise DuplicateKeyError(f"clave duplicada en índice único: {self.display(key)}")
            last_key = key
            if not self._is_char:
                self._track_bounds(key)
            if current is None or len(current.keys) >= per_leaf:
                new_leaf = LeafNode(self.dm.allocate_page())
                if current is not None:
                    current.next_leaf = new_leaf.page_id
                    new_leaf.prev_leaf = current.page_id
                    self._write_leaf(current)
                current = new_leaf
                level.append((key, new_leaf.page_id))
            current.keys.append(key)
            current.pids.append(rid.page_id)
            current.slots.append(rid.slot)
            self.entry_count += 1
        if current is None:
            return
        self._write_leaf(current)
        self.leaf_pages = len(level)
        self.height = 1
        per_node = max(3, int(self.fan_out * fill_factor))
        while len(level) > 1:
            # Reparto parejo de los hijos: con per_node >= 3 cada nodo recibe >= 2 hijos.
            nodes = -(-len(level) // per_node)
            base, extra = divmod(len(level), nodes)
            parents: list[tuple[Any, int]] = []
            start = 0
            for i in range(nodes):
                size = base + (1 if i < extra else 0)
                group = level[start : start + size]
                start += size
                node = InternalNode(self.dm.allocate_page(), [k for k, _ in group[1:]], [pid for _, pid in group])
                self._write_internal(node)
                parents.append((group[0][0], node.page_id))
            level = parents
            self.height += 1
        self.root = level[0][1]
        self._dirty = self._stats_dirty = True

    # ------------------------------------------------------------------ búsqueda
    def _descend(self, key: Any | None) -> tuple[LeafNode | None, Any]:
        """Baja a la hoja más a la izquierda que puede contener ``key`` (h lecturas).

        Devuelve también la cota superior de esa hoja (el separador a su derecha):
        todas las hojas siguientes tienen claves ``>=`` esa cota, lo que permite no
        leer la hoja vecina cuando la búsqueda ya terminó.
        """
        if self.root == NULL_PAGE:
            return None, None
        # Sin duplicados la clave solo puede estar en la hoja cuyo rango
        # [K(i-1), K(i)) la contiene: se baja por bisect_right y se evita leer la
        # hoja vecina cuando la clave buscada coincide con un separador.
        pick = bisect_right if self.unique else bisect_left
        upper = None
        node = self._read_node(self.root)
        while isinstance(node, InternalNode):
            i = 0 if key is None else pick(node.keys, key)
            if i < len(node.keys):
                upper = node.keys[i]
            node = self._read_node(node.children[i])
        return node, upper

    def range_search(
        self,
        low: Any = None,
        high: Any = None,
        *,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
    ) -> Iterator[tuple[Any, RID]]:
        """Entradas con ``low <= key <= high`` en orden (cotas opcionales y abiertas/cerradas).

        Costo: h lecturas para bajar + 1 por cada hoja adicional recorrida.
        """
        lo = None if low is None else self.comparable(low)
        hi = None if high is None else self.comparable(high)
        leaf, upper = self._descend(lo)
        if leaf is None:
            return
        if lo is None:
            pos = 0
        else:
            pos = bisect_left(leaf.keys, lo) if low_inclusive else bisect_right(leaf.keys, lo)
        while True:
            keys = leaf.keys
            n = len(keys)
            while pos < n:
                k = keys[pos]
                if hi is not None and (k > hi or (k == hi and not high_inclusive)):
                    return
                yield self.display(k), RID(leaf.pids[pos], leaf.slots[pos])
                pos += 1
            if leaf.next_leaf == NULL_PAGE:
                return
            # La hoja siguiente solo tiene claves >= upper: si ya superan high, no se lee.
            if hi is not None and upper is not None and (upper > hi or (upper == hi and not high_inclusive)):
                return
            upper = None
            nxt = self._read_node(leaf.next_leaf)
            assert isinstance(nxt, LeafNode)
            leaf, pos = nxt, 0
            if lo is not None and not low_inclusive:
                pos = bisect_right(leaf.keys, lo)

    def search(self, value: Any) -> list[RID]:
        """RIDs con ``key == value`` (h lecturas si no hay duplicados que crucen hojas)."""
        return [rid for _, rid in self.range_search(value, value)]

    # ------------------------------------------------------------------ borrado
    def delete(self, value: Any, rid: RID) -> bool:
        """Quita la entrada ``(value, rid)``; sin merge ni redistribución."""
        key = self.comparable(value)
        leaf, _ = self._descend(key)
        if leaf is None:
            return False
        pos = bisect_left(leaf.keys, key)
        while True:
            while pos < len(leaf.keys) and leaf.keys[pos] == key:
                if leaf.pids[pos] == rid.page_id and leaf.slots[pos] == rid.slot:
                    del leaf.keys[pos], leaf.pids[pos], leaf.slots[pos]
                    self._write_leaf(leaf)
                    self.entry_count -= 1
                    self._stats_dirty = True
                    return True
                pos += 1
            if pos < len(leaf.keys) or leaf.next_leaf == NULL_PAGE:
                return False
            nxt = self._read_node(leaf.next_leaf)
            assert isinstance(nxt, LeafNode)
            leaf, pos = nxt, 0

    # ------------------------------------------------------------------ verificación
    def iter_all(self) -> Iterator[tuple[Any, RID]]:
        return self.range_search()

    def check_invariants(self) -> dict[str, int]:
        """Verifica la estructura completa (para tests). Devuelve estadísticas."""
        if self.root == NULL_PAGE:
            assert self.height == 0 and self.entry_count == 0
            return {"height": 0, "leaves": 0, "internals": 0, "entries": 0}
        leaves: list[LeafNode] = []
        internals = 0

        def visit(page_id: int, depth: int, lo: Any, hi: Any) -> None:
            nonlocal internals
            node = self._read_node(page_id)
            assert all(a <= b for a, b in zip(node.keys, node.keys[1:])), f"claves desordenadas en {page_id}"
            if node.keys:
                assert lo is None or node.keys[0] >= lo, f"cota inferior violada en {page_id}"
                assert hi is None or node.keys[-1] <= hi, f"cota superior violada en {page_id}"
            if isinstance(node, LeafNode):
                assert depth == self.height, f"hoja {page_id} a profundidad {depth} != {self.height}"
                assert len(node.keys) <= self.leaf_capacity
                leaves.append(node)
                return
            internals += 1
            assert 1 <= len(node.keys) <= self.internal_capacity
            assert len(node.children) == len(node.keys) + 1
            bounds = [lo, *node.keys, hi]
            for i, child in enumerate(node.children):
                visit(child, depth + 1, bounds[i], bounds[i + 1])

        visit(self.root, 1, None, None)
        # Enlace horizontal: el recorrido por next_leaf visita las hojas en el mismo orden.
        for a, b in zip(leaves, leaves[1:]):
            assert a.next_leaf == b.page_id and b.prev_leaf == a.page_id, "enlace de hojas roto"
        assert leaves[0].prev_leaf == NULL_PAGE and leaves[-1].next_leaf == NULL_PAGE
        entries = sum(len(leaf.keys) for leaf in leaves)
        assert entries == self.entry_count, f"{entries} entradas != {self.entry_count}"
        assert len(leaves) == self.leaf_pages
        return {"height": self.height, "leaves": len(leaves), "internals": internals, "entries": entries}
