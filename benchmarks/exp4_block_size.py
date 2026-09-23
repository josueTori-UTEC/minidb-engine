"""Experimento 4: sensibilidad al tamaño de bloque.

Para B ∈ {1024, 2048, 4096, 8192} se insertan N = 100 000 registros en un Heap con
un índice B+ único sobre ``id`` y luego se hacen 1000 búsquedas puntuales. Se
reporta la capacidad de hojas e internos (fan-out), la altura h, las páginas
usadas y el total de I/O (inserción + búsquedas), además del volumen de bytes
transferidos (I/O × B). Como referencia se incluyen el Sequential (M y costo de la
búsqueda binaria) y el hash (profundidad global).

Uso: ``python -m benchmarks.exp4_block_size [--quick]``
"""

from __future__ import annotations

import argparse
import math
import random
import time
from typing import Any

from backend.files.heap_file import HeapFile
from backend.files.sequential_file import SequentialFile
from backend.indexes.bplus_tree import BPlusTree
from backend.indexes.extendible_hash import ExtendibleHash
from backend.storage.disk_manager import DiskCounter
from benchmarks.common import (
    EMPLEADOS,
    ID_COLUMN,
    cleanup,
    fresh_dir,
    log2_ceil,
    new_figure,
    no_gc,
    rows,
    save_figure,
    style_axis,
    write_csv,
)

BLOCK_SIZES = [1024, 2048, 4096, 8192]
N = 100_000
QUICK_N = 20_000
SEARCHES = 1000
QUICK_SEARCHES = 200
FIELDS = [
    "page_size", "n", "records_per_page", "heap_pages", "leaf_capacity", "internal_capacity", "fan_out",
    "height", "height_min_theory", "height_max_theory", "leaf_pages", "internal_pages", "index_pages",
    "insert_reads", "insert_writes", "insert_io", "insert_time_s", "index_insert_io",
    "search_reads_total", "search_reads_mean", "search_ms_mean", "total_io", "mb_transferred",
    "seq_main_pages", "seq_search_reads_mean", "hash_global_depth", "hash_buckets", "hash_search_reads_mean",
]


def _height_bounds(n: int, leaf_cap: int, fan_out: int) -> tuple[int, int]:
    """Altura mínima (nodos llenos) y máxima (nodos al 50 %, lo que garantiza el split)."""
    h_min = 1 + max(0, math.ceil(math.log(math.ceil(n / leaf_cap), fan_out))) if n > leaf_cap else 1
    leaves_max = math.ceil(n / max(1, leaf_cap // 2))
    h_max = 1 + max(0, math.ceil(math.log(leaves_max, max(2, (fan_out + 1) // 2)))) if leaves_max > 1 else 1
    return h_min, h_max


def run_block(b: int, n: int, searches: int) -> dict[str, Any]:
    d = fresh_dir(f"exp4_{b}")
    heap_c, idx_c, other_c = DiskCounter(), DiskCounter(), DiskCounter()
    heap = HeapFile.create(d / "t.heap", EMPLEADOS, b, heap_c)
    bpt = BPlusTree.create(d / "t.bpt", ID_COLUMN, b, idx_c, unique=True)
    hsh = ExtendibleHash.create(d / "t.hsh", ID_COLUMN, b, other_c, unique=True)
    seq = SequentialFile.create(d / "t.seq", d / "t.ovf", EMPLEADOS, b, other_c)
    heap_c.reset()
    idx_c.reset()
    insert_s = 0.0
    clock = time.perf_counter
    with no_gc():
        for row in rows(n):
            t0 = clock()
            rid = heap.insert(row)
            bpt.insert(row[0], rid)
            insert_s += clock() - t0
            hsh.insert(row[0], rid)  # referencia: su I/O va a otro contador
        t0 = clock()
        heap.flush()
        bpt.flush()
        insert_s += clock() - t0
    hr, hw = heap_c.snapshot()
    ir, iw = idx_c.snapshot()
    for row in rows(n):  # Sequential de referencia (no se cuenta en el total del B+)
        seq.insert(row, check_unique=False)
        if seq.needs_reorganization():
            seq.reorganize()
    seq.reorganize()

    keys = random.Random(b).sample(range(1, n + 1), searches)
    heap_c.reset()
    idx_c.reset()
    t0 = time.perf_counter()
    with no_gc():
        for k in keys:
            (rid,) = bpt.search(k)
            heap.read(rid)
    search_ms = (time.perf_counter() - t0) * 1000 / searches
    search_reads = heap_c.disk_reads + idx_c.disk_reads

    other_c.reset()
    for k in keys:
        next(seq.find(k))
    seq_reads = other_c.disk_reads / searches
    other_c.reset()
    for k in keys:
        hsh.search(k)
    hash_reads = other_c.disk_reads / searches + 1  # + traer el registro

    h_min, h_max = _height_bounds(n, bpt.leaf_capacity, bpt.fan_out)
    insert_io = hr + hw + ir + iw
    out = {
        "page_size": b, "n": n, "records_per_page": heap.capacity, "heap_pages": heap.page_count,
        "leaf_capacity": bpt.leaf_capacity, "internal_capacity": bpt.internal_capacity, "fan_out": bpt.fan_out,
        "height": bpt.height, "height_min_theory": h_min, "height_max_theory": h_max,
        "leaf_pages": bpt.leaf_pages, "internal_pages": bpt.page_count - bpt.leaf_pages,
        "index_pages": bpt.page_count, "insert_reads": hr + ir, "insert_writes": hw + iw, "insert_io": insert_io,
        "insert_time_s": insert_s, "index_insert_io": ir + iw, "search_reads_total": search_reads,
        "search_reads_mean": search_reads / searches, "search_ms_mean": search_ms,
        "total_io": insert_io + search_reads, "mb_transferred": (insert_io + search_reads) * b / 2**20,
        "seq_main_pages": seq.main_pages, "seq_search_reads_mean": seq_reads,
        "hash_global_depth": hsh.global_depth, "hash_buckets": hsh.bucket_pages, "hash_search_reads_mean": hash_reads,
    }
    for s in (heap, bpt, hsh, seq):
        s.close()
    cleanup(f"exp4_{b}")
    return out


def run(quick: bool = False) -> list[dict[str, Any]]:
    n = QUICK_N if quick else N
    searches = QUICK_SEARCHES if quick else SEARCHES
    results = []
    for b in BLOCK_SIZES:
        r = run_block(b, n, searches)
        results.append(r)
        print(f"  exp4 B={b:>5}: fan-out {r['fan_out']:>5}  h={r['height']}  hojas={r['leaf_pages']:>5}  "
              f"I/O inserción={r['insert_io']:>8}  lecturas/búsqueda={r['search_reads_mean']:.2f}  "
              f"MB={r['mb_transferred']:.1f}", flush=True)
    write_csv("exp4_block_size.csv", FIELDS, ([r[f] for f in FIELDS] for r in results))
    plot(results)
    return results


def plot(results: list[dict[str, Any]]) -> None:
    bs = [r["page_size"] for r in results]
    labels = [str(b) for b in bs]
    fig, axes = new_figure(ncols=2, width=5.6)
    ax = axes[0]
    ax.bar(labels, [r["fan_out"] for r in results], color="#55A868", alpha=0.8, label="fan-out (M + 1)")
    ax.bar(labels, [r["leaf_capacity"] for r in results], color="#8172B3", alpha=0.6, width=0.45,
           label="entradas por hoja")
    style_axis(ax, "Exp. 4 — Fan-out y altura del B+", "tamaño de página B (bytes)", "capacidad del nodo", logy=True)
    ax2 = ax.twinx()
    ax2.plot(labels, [r["height"] for r in results], "o-", color="#C44E52", lw=2, label="altura h (medida)")
    ax2.fill_between(labels, [r["height_min_theory"] for r in results], [r["height_max_theory"] for r in results],
                     color="#C44E52", alpha=0.12, label="h teórica [llenos, 50 %]")
    ax2.set_ylabel("altura h")
    ax2.set_ylim(0, max(r["height_max_theory"] for r in results) + 1)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="upper left")

    ax = axes[1]
    heap_io = [r["insert_io"] - r["index_insert_io"] for r in results]
    index_io = [r["index_insert_io"] for r in results]
    search_io = [r["search_reads_total"] for r in results]
    ax.bar(labels, heap_io, color="#4C72B0", label="inserción: heap")
    ax.bar(labels, index_io, bottom=heap_io, color="#55A868", label="inserción: B+")
    ax.bar(labels, search_io, bottom=[a + b for a, b in zip(heap_io, index_io)], color="#DD8452",
           label="1000 búsquedas (h + 1)")
    for i, r in enumerate(results):
        ax.text(i, r["total_io"] * 1.01, f"{r['total_io']:,}", ha="center", fontsize=7)
    style_axis(ax, "Exp. 4 — I/O totales vs B", "tamaño de página B (bytes)", "páginas transferidas")
    ax.set_ylim(0, max(r["total_io"] for r in results) * 1.15)
    ax3 = ax.twinx()
    ax3.plot(labels, [r["mb_transferred"] for r in results], "^--", color="gray", label="MB transferidos (I/O × B)")
    ax3.set_ylabel("MB transferidos")
    ax3.set_ylim(0, max(r["mb_transferred"] for r in results) * 1.15)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax3.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="lower left")
    save_figure(fig, "exp4_block_size.png")

    fig, ax = new_figure()
    ax.plot(labels, [r["search_reads_mean"] for r in results], "o-", color="#55A868", label="B+ (h + 1)")
    ax.plot(labels, [r["seq_search_reads_mean"] for r in results], "s-", color="#C44E52",
            label="Sequential (⌈log2 M⌉)")
    ax.plot(labels, [r["hash_search_reads_mean"] for r in results], "^-", color="#8172B3", label="Hash (2 + 1)")
    ax.plot(labels, [log2_ceil(r["seq_main_pages"]) for r in results], ":", color="#C44E52", lw=1)
    style_axis(ax, "Exp. 4 — Lecturas por búsqueda puntual vs B", "tamaño de página B (bytes)",
               "lecturas promedio")
    ax.legend(fontsize=8)
    save_figure(fig, "exp4_search_reads.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true")
    run(quick=parser.parse_args().quick)


if __name__ == "__main__":
    main()
