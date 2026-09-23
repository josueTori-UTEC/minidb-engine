"""Experimento 1: costo de inserción masiva.

Inserta lotes crecientes N ∈ {1e3, 1e4, 5e4, 1e5, 2.5e5, 5e5} en archivos nuevos
(B = 4096) y mide tiempo total, lecturas y escrituras para:

* Heap File (free-list).
* Sequential File sin reorganización (todo lo que no cabe va al overflow).
* Sequential File con reorganización automática (umbral 10 % de páginas
  principales, fill factor 0.75).
* Árbol B+ y Hash extensible como índices únicos sobre ``id`` de un Heap: se
  reporta el costo del índice solo y el total (heap + índice).

Se mide la inserción estructural (sin validar unicidad de la PK) en todas las
organizaciones: las claves generadas son únicas por construcción y así se
compara lo mismo en todas. Además se mide el costo de una búsqueda puntual tras
la carga en las dos variantes del Sequential, para mostrar qué compra la
reorganización.

Uso: ``python -m benchmarks.exp1_insert [--quick]``
"""

from __future__ import annotations

import argparse
import random
import time
from typing import Any

from backend.files.heap_file import HeapFile
from backend.files.sequential_file import SequentialFile
from backend.indexes.bplus_tree import BPlusTree
from backend.indexes.extendible_hash import ExtendibleHash
from backend.storage.disk_manager import DiskCounter
from benchmarks.common import (
    COLORS,
    EMPLEADOS,
    ID_COLUMN,
    Measure,
    cleanup,
    fresh_dir,
    load_csv,
    log2_ceil,
    new_figure,
    no_gc,
    rows,
    save_figure,
    style_axis,
    write_csv,
)

SIZES = [1_000, 10_000, 50_000, 100_000, 250_000, 500_000]
QUICK_SIZES = [1_000, 5_000, 20_000]
PAGE_SIZE = 4096
SEARCH_SAMPLES = 100
STRUCTURES = ["Heap", "Sequential sin reorg", "Sequential con reorg", "B+", "Hash"]
FIELDS = [
    "structure", "n", "time_s", "reads", "writes", "total_io", "io_per_insert", "us_per_insert", "file_pages",
    "index_time_s", "index_reads", "index_writes", "index_io_per_insert", "height", "global_depth",
    "main_pages", "overflow_pages", "reorganizations", "search_reads_mean",
]


def _base(structure: str, n: int, seconds: float, reads: int, writes: int, pages: int) -> dict[str, Any]:
    return {
        "structure": structure, "n": n, "time_s": seconds, "reads": reads, "writes": writes,
        "total_io": reads + writes, "io_per_insert": (reads + writes) / n, "us_per_insert": seconds / n * 1e6,
        "file_pages": pages,
    }


def run_heap(n: int) -> dict[str, Any]:
    d = fresh_dir("exp1_heap")
    counter = DiskCounter()
    heap = HeapFile.create(d / "t.heap", EMPLEADOS, PAGE_SIZE, counter)
    counter.reset()
    with no_gc(), Measure(counter) as m:
        for row in rows(n):
            heap.insert(row)
        heap.flush()
    out = _base("Heap", n, m.seconds, m.reads, m.writes, heap.page_count)
    heap.close()
    cleanup("exp1_heap")
    return out


def run_sequential(n: int, reorganize: bool) -> dict[str, Any]:
    name = "Sequential con reorg" if reorganize else "Sequential sin reorg"
    d = fresh_dir("exp1_seq")
    counter = DiskCounter()
    seq = SequentialFile.create(d / "t.seq", d / "t.ovf", EMPLEADOS, PAGE_SIZE, counter, auto_reorganize=reorganize)
    counter.reset()
    with no_gc(), Measure(counter) as m:
        for row in rows(n):
            seq.insert(row, check_unique=False)
            if seq.needs_reorganization():
                seq.reorganize()
        seq.flush()
    out = _base(name, n, m.seconds, m.reads, m.writes, seq.main_pages + seq.overflow_pages)
    out.update(main_pages=seq.main_pages, overflow_pages=seq.overflow_pages, reorganizations=seq.reorganizations)
    keys = random.Random(n).sample(range(1, n + 1), min(SEARCH_SAMPLES, n))
    search_reads = []
    for k in keys:
        r0 = counter.disk_reads
        assert next(seq.find(k), None) is not None
        search_reads.append(counter.disk_reads - r0)
    out["search_reads_mean"] = sum(search_reads) / len(search_reads)
    seq.close()
    cleanup("exp1_seq")
    return out


def run_index(n: int, kind: str) -> dict[str, Any]:
    d = fresh_dir("exp1_idx")
    heap_counter, idx_counter = DiskCounter(), DiskCounter()
    heap = HeapFile.create(d / "t.heap", EMPLEADOS, PAGE_SIZE, heap_counter)
    if kind == "B+":
        index: BPlusTree | ExtendibleHash = BPlusTree.create(d / "t.bpt", ID_COLUMN, PAGE_SIZE, idx_counter, unique=True)
    else:
        index = ExtendibleHash.create(d / "t.hsh", ID_COLUMN, PAGE_SIZE, idx_counter, unique=True)
    heap_counter.reset()
    idx_counter.reset()
    heap_s = idx_s = 0.0
    clock = time.perf_counter
    with no_gc():
        for row in rows(n):
            t0 = clock()
            rid = heap.insert(row)
            t1 = clock()
            index.insert(row[0], rid)
            t2 = clock()
            heap_s += t1 - t0
            idx_s += t2 - t1
        t0 = clock()
        heap.flush()
        t1 = clock()
        index.flush()
        heap_s += t1 - t0
        idx_s += clock() - t1
    hr, hw = heap_counter.snapshot()
    ir, iw = idx_counter.snapshot()
    out = _base(kind, n, heap_s + idx_s, hr + ir, hw + iw, heap.page_count + index.page_count)
    out.update(
        index_time_s=idx_s, index_reads=ir, index_writes=iw, index_io_per_insert=(ir + iw) / n,
        height=index.height if isinstance(index, BPlusTree) else None,
        global_depth=index.global_depth if isinstance(index, ExtendibleHash) else None,
    )
    heap.close()
    index.close()
    cleanup("exp1_idx")
    return out


def run(quick: bool = False) -> list[dict[str, Any]]:
    sizes = QUICK_SIZES if quick else SIZES
    results: list[dict[str, Any]] = []
    for n in sizes:
        for fn in (
            lambda: run_heap(n),
            lambda: run_sequential(n, reorganize=False),
            lambda: run_sequential(n, reorganize=True),
            lambda: run_index(n, "B+"),
            lambda: run_index(n, "Hash"),
        ):
            r = fn()
            results.append(r)
            print(
                f"  exp1 {r['structure']:<22} N={n:>7}: {r['time_s']:8.2f} s  "
                f"R={r['reads']:>9} W={r['writes']:>9}  ({r['io_per_insert']:.2f} I/O por inserción)",
                flush=True,
            )
    write_csv("exp1_insert.csv", FIELDS, ([r.get(f) for f in FIELDS] for r in results))
    plot(results)
    return results


def plot(results: list[dict[str, Any]]) -> None:
    by = {s: [r for r in results if r["structure"] == s] for s in STRUCTURES}
    sizes = sorted({r["n"] for r in results})

    for metric, title, ylabel, fname in (
        ("time_s", "Tiempo total de inserción", "tiempo (s)", "exp1_time.png"),
        ("writes", "Escrituras de páginas", "páginas escritas", "exp1_writes.png"),
        ("reads", "Lecturas de páginas", "páginas leídas", "exp1_reads.png"),
    ):
        fig, ax = new_figure()
        for s in STRUCTURES:
            data = by[s]
            ax.plot([r["n"] for r in data], [max(r[metric], 1e-9) for r in data], "o-", color=COLORS[s],
                    label=f"{s} (total)" if s in ("B+", "Hash") else s)
            if s in ("B+", "Hash"):
                key = {"time_s": "index_time_s", "writes": "index_writes", "reads": "index_reads"}[metric]
                ax.plot([r["n"] for r in data], [max(r[key], 1e-9) for r in data], "s--", color=COLORS[s],
                        alpha=0.7, label=f"{s} (solo índice)")
        style_axis(ax, f"Exp. 1 — {title} (B = {PAGE_SIZE})", "N (registros insertados)", ylabel, logx=True, logy=True)
        ax.legend(fontsize=8)
        save_figure(fig, fname)

    # I/O por inserción vs costo teórico
    fig, ax = new_figure(width=7.2)
    for s in STRUCTURES:
        data = by[s]
        ax.plot([r["n"] for r in data], [r["io_per_insert"] for r in data], "o-", color=COLORS[s], label=s)
    heap_theory = 2
    ax.axhline(heap_theory, color=COLORS["Heap"], ls=":", lw=1)
    ax.text(sizes[0], heap_theory * 1.04, "Heap teórico: 1R + 1W", fontsize=7, color=COLORS["Heap"])
    bp = by["B+"]
    ax.plot([r["n"] for r in bp], [2 + (r["height"] or 1) + 1 for r in bp], ":", color=COLORS["B+"], lw=1,
            label="B+ teórico: heap 2 + (h R + 1W)")
    ax.axhline(2 + 3, color=COLORS["Hash"], ls=":", lw=1, label="Hash teórico: heap 2 + (2R + 1W)")
    seq = by["Sequential con reorg"]
    ax.plot([r["n"] for r in seq], [log2_ceil(max(r["main_pages"], 1)) + 1 for r in seq], ":",
            color=COLORS["Sequential con reorg"], lw=1, label="Sequential: cota ⌈log2 M_final⌉ + 1W")
    ax.axhline(3, color=COLORS["Sequential sin reorg"], ls=":", lw=1,
               label="Sequential sin reorg: 1R + 1R overflow + 1W")
    style_axis(ax, "Exp. 1 — I/O por inserción (medido vs teórico)", "N", "I/O por inserción", logx=True)
    ax.legend(fontsize=7, ncol=2)
    save_figure(fig, "exp1_io_per_insert.png")

    # Qué compra la reorganización: costo de búsqueda después de cargar
    fig, axes = new_figure(ncols=2, width=5.2)
    for s in ("Sequential sin reorg", "Sequential con reorg"):
        data = by[s]
        axes[0].plot([r["n"] for r in data], [r["search_reads_mean"] for r in data], "o-", color=COLORS[s], label=s)
        axes[1].plot([r["n"] for r in data], [r["overflow_pages"] for r in data], "o-", color=COLORS[s], label=s)
    seq = by["Sequential con reorg"]
    axes[0].plot([r["n"] for r in seq], [log2_ceil(max(r["main_pages"], 1)) for r in seq], ":", color="gray",
                 label="⌈log2 M⌉ (con reorg)")
    style_axis(axes[0], "Búsqueda puntual tras la carga", "N", "lecturas promedio", logx=True, logy=True)
    style_axis(axes[1], "Páginas de overflow al final", "N", "páginas de overflow", logx=True)
    axes[1].set_yscale("symlog", linthresh=1)  # con reorganización puede quedar en 0
    axes[0].legend(fontsize=8)
    axes[1].legend(fontsize=8)
    save_figure(fig, "exp1_sequential_reorg.png")


def replot() -> None:
    plot(load_csv("exp1_insert.csv"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="tamaños pequeños para una corrida rápida")
    args = parser.parse_args()
    run(quick=args.quick)


if __name__ == "__main__":
    main()
