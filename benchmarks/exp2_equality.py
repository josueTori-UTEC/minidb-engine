"""Experimento 2: búsquedas puntuales de igualdad.

N = 100 000 registros (B = 4096) y 1000 claves aleatorias existentes. Para cada
clave se mide lecturas y milisegundos de:

* Full Scan sobre el Heap (P lecturas, sin cortar al encontrar la clave);
* búsqueda binaria sobre el Sequential File reorganizado;
* B+ único sobre ``id`` (h lecturas + 1 para traer el registro por RID);
* Hash extensible único sobre ``id`` (directorio + bucket + registro).

Se reporta media y desviación estándar, junto al costo teórico.

Uso: ``python -m benchmarks.exp2_equality [--quick]``
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import time
from typing import Any, Callable

from backend.files.heap_file import HeapFile
from backend.files.sequential_file import SequentialFile
from backend.indexes.bplus_tree import BPlusTree
from backend.indexes.extendible_hash import ExtendibleHash
from backend.storage.disk_manager import DiskCounter
from benchmarks.common import (
    COLORS,
    EMPLEADOS,
    ID_COLUMN,
    cleanup,
    fresh_dir,
    load_csv,
    mean_std,
    new_figure,
    no_gc,
    rows,
    save_figure,
    style_axis,
    write_csv,
)

N = 100_000
QUICK_N = 20_000
QUERIES = 1000
QUICK_QUERIES = 100
PAGE_SIZE = 4096
METHODS = ["Full Scan (Heap)", "Búsqueda binaria (Sequential)", "B+ (IndexScan)", "Hash (IndexScan)"]
_ID = struct.Struct("<i")


class Structures:
    """Heap + índices B+ y hash sobre ``id`` + Sequential reorganizado, con los mismos datos."""

    def __init__(self, n: int, name: str) -> None:
        d = fresh_dir(name)
        self.counter = DiskCounter()
        c = self.counter
        self.heap = HeapFile.create(d / "t.heap", EMPLEADOS, PAGE_SIZE, c)
        self.bpt = BPlusTree.create(d / "t.bpt", ID_COLUMN, PAGE_SIZE, c, unique=True)
        self.hsh = ExtendibleHash.create(d / "t.hsh", ID_COLUMN, PAGE_SIZE, c, unique=True)
        self.seq = SequentialFile.create(d / "t.seq", d / "t.ovf", EMPLEADOS, PAGE_SIZE, c)
        for row in rows(n):
            rid = self.heap.insert(row)
            self.bpt.insert(row[0], rid)
            self.hsh.insert(row[0], rid)
            self.seq.insert(row, check_unique=False)
            if self.seq.needs_reorganization():
                self.seq.reorganize()
        self.seq.reorganize()  # archivo principal limpio, overflow vacío
        for s in (self.heap, self.bpt, self.hsh, self.seq):
            s.flush()
        self.name = name

    def close(self) -> None:
        for s in (self.heap, self.bpt, self.hsh, self.seq):
            s.close()
        cleanup(self.name)


def _measure(counter: DiskCounter, fn: Callable[[], Any]) -> tuple[int, float, Any]:
    r0 = counter.disk_reads
    t0 = time.perf_counter()
    out = fn()
    ms = (time.perf_counter() - t0) * 1000
    return counter.disk_reads - r0, ms, out


def run(quick: bool = False) -> list[dict[str, Any]]:
    n = QUICK_N if quick else N
    q = QUICK_QUERIES if quick else QUERIES
    st = Structures(n, "exp2")
    keys = random.Random(2026).sample(range(1, n + 1), q)
    unpack = _ID.unpack_from
    raw: list[tuple[str, int, int, float]] = []
    methods: dict[str, Callable[[int], Any]] = {
        "Full Scan (Heap)": lambda k: [v for _, v in st.heap.scan(lambda buf, off, k=k: unpack(buf, off)[0] == k)],
        "Búsqueda binaria (Sequential)": lambda k: [v for _, v in st.seq.find(k)],
        "B+ (IndexScan)": lambda k: [st.heap.read(rid) for rid in st.bpt.search(k)],
        "Hash (IndexScan)": lambda k: [st.heap.read(rid) for rid in st.hsh.search(k)],
    }
    with no_gc():
        for method, fn in methods.items():
            for k in keys:
                reads, ms, out = _measure(st.counter, lambda fn=fn, k=k: fn(k))
                assert len(out) == 1 and out[0][0] == k, (method, k)
                raw.append((method, k, reads, ms))
    theory = {
        "Full Scan (Heap)": st.heap.page_count,
        # La bisección sobre M páginas hace ⌊log2 M⌋ o ⌈log2 M⌉ sondeos: en promedio log2 M.
        "Búsqueda binaria (Sequential)": round(math.log2(st.seq.main_pages), 2),
        "B+ (IndexScan)": st.bpt.height + 1,
        "Hash (IndexScan)": 3,
    }
    params = {
        "Full Scan (Heap)": f"P = {st.heap.page_count} páginas",
        "Búsqueda binaria (Sequential)": f"M = {st.seq.main_pages} páginas principales",
        "B+ (IndexScan)": f"h = {st.bpt.height}",
        "Hash (IndexScan)": f"global depth = {st.hsh.global_depth}",
    }
    summary = []
    for method in METHODS:
        reads = [r for m, _, r, _ in raw if m == method]
        mss = [t for m, _, _, t in raw if m == method]
        mr, sr = mean_std(reads)
        mm, sm = mean_std(mss)
        summary.append({
            "method": method, "n": n, "queries": q, "mean_reads": mr, "std_reads": sr, "min_reads": min(reads),
            "max_reads": max(reads), "mean_ms": mm, "std_ms": sm, "theoretical_reads": theory[method],
            "parameters": params[method],
        })
        print(f"  exp2 {method:<30} reads {mr:9.2f} ± {sr:6.2f}   {mm:8.3f} ± {sm:6.3f} ms  "
              f"(teórico {theory[method]})", flush=True)
    st.close()
    fields = ["method", "n", "queries", "mean_reads", "std_reads", "min_reads", "max_reads", "mean_ms", "std_ms",
              "theoretical_reads", "parameters"]
    write_csv("exp2_equality.csv", fields, ([s[f] for f in fields] for s in summary))
    write_csv("exp2_equality_raw.csv", ["method", "key", "reads", "ms"], raw)
    plot(summary)
    return summary


def plot(summary: list[dict[str, Any]]) -> None:
    fig, axes = new_figure(ncols=2, width=5.6)
    labels = ["Full Scan\n(Heap)", "Búsqueda\nbinaria (Seq)", "B+", "Hash"]
    colors = [COLORS[s["method"]] for s in summary]
    x = range(len(summary))
    axes[0].bar(x, [s["mean_reads"] for s in summary], yerr=[s["std_reads"] for s in summary], color=colors,
                capsize=4)
    axes[0].scatter(x, [s["theoretical_reads"] for s in summary], marker="_", s=900, color="black", zorder=3,
                    label="costo teórico")
    for i, s in enumerate(summary):
        axes[0].text(i, s["mean_reads"] * 1.15, f"{s['mean_reads']:.1f}", ha="center", fontsize=8)
    axes[1].bar(x, [s["mean_ms"] for s in summary], yerr=[s["std_ms"] for s in summary], color=colors, capsize=4)
    for i, s in enumerate(summary):
        axes[1].text(i, s["mean_ms"] * 1.15, f"{s['mean_ms']:.3f}", ha="center", fontsize=8)
    n = summary[0]["n"]
    style_axis(axes[0], f"Exp. 2 — Lecturas por búsqueda (N = {n:,})", "", "lecturas (media ± σ)", logy=True)
    style_axis(axes[1], f"Exp. 2 — Latencia por búsqueda ({summary[0]['queries']} consultas)", "", "ms (media ± σ)",
               logy=True)
    for ax in axes:
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, fontsize=8)
    axes[0].legend(fontsize=8)
    save_figure(fig, "exp2_equality.png")


def replot() -> None:
    plot(load_csv("exp2_equality.csv"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true")
    run(quick=parser.parse_args().quick)


if __name__ == "__main__":
    main()
