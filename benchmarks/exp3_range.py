"""Experimento 3: búsquedas por rango con selectividad variable.

N = 100 000 registros (B = 4096). Para selectividades de 0.1 %, 1 %, 5 %, 10 % y
25 % se consultan rangos aleatorios ``id BETWEEN a AND a + k - 1`` (10
repeticiones por selectividad) con:

* B+ (IndexRangeScan no agrupado sobre el Heap: 1 lectura por registro);
* Sequential File (búsqueda binaria de la cota + páginas contiguas);
* Full Scan del Heap (P lecturas siempre).

Uso: ``python -m benchmarks.exp3_range [--quick]``
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import time
from typing import Any, Callable

from benchmarks.common import (
    COLORS,
    load_csv,
    mean_std,
    new_figure,
    no_gc,
    save_figure,
    style_axis,
    write_csv,
)
from benchmarks.exp2_equality import Structures

N = 100_000
QUICK_N = 20_000
SELECTIVITIES = [0.001, 0.01, 0.05, 0.10, 0.25]
REPETITIONS = 10
QUICK_REPETITIONS = 3
METHODS = ["B+", "Sequential", "Full Scan (Heap)"]
_ID = struct.Struct("<i")


def run(quick: bool = False) -> list[dict[str, Any]]:
    n = QUICK_N if quick else N
    reps = QUICK_REPETITIONS if quick else REPETITIONS
    st = Structures(n, "exp3")
    rng = random.Random(3)
    unpack = _ID.unpack_from
    counter = st.counter

    def full_scan(a: int, b: int) -> int:
        return sum(1 for _ in st.heap.scan(lambda buf, off: a <= unpack(buf, off)[0] <= b))

    methods: dict[str, Callable[[int, int], int]] = {
        "B+": lambda a, b: sum(1 for _, rid in st.bpt.range_search(a, b) if st.heap.read(rid)),
        "Sequential": lambda a, b: sum(1 for _ in st.seq.range_scan(a, b)),
        "Full Scan (Heap)": full_scan,
    }
    per_leaf = st.bpt.entry_count / st.bpt.leaf_pages
    rpp = st.seq.records_per_page_after_reorg
    raw = []
    summary = []
    with no_gc():
        for sel in SELECTIVITIES:
            k = max(1, int(sel * n))
            ranges = [(a, a + k - 1) for a in (rng.randint(1, n - k + 1) for _ in range(reps))]
            for method in METHODS:
                fn = methods[method]
                reads, mss = [], []
                for a, b in ranges:
                    r0 = counter.disk_reads
                    t0 = time.perf_counter()
                    got = fn(a, b)
                    mss.append((time.perf_counter() - t0) * 1000)
                    reads.append(counter.disk_reads - r0)
                    assert got == k, (method, a, b, got)
                    raw.append((method, sel, a, b, reads[-1], mss[-1]))
                theory = {
                    # h para bajar + hojas adicionales + 1 lectura por registro (no agrupado)
                    "B+": st.bpt.height + math.ceil(k / per_leaf) - 1 + k,
                    # bisección (log2 M en promedio) + k/r páginas contiguas (la primera ya se leyó)
                    "Sequential": round(math.log2(st.seq.main_pages) + k / rpp, 1),
                    "Full Scan (Heap)": st.heap.page_count,
                }[method]
                mr, sr = mean_std(reads)
                mm, sm = mean_std(mss)
                summary.append({
                    "method": method, "selectivity": sel, "k": k, "repetitions": reps, "mean_reads": mr,
                    "std_reads": sr, "mean_ms": mm, "std_ms": sm, "theoretical_reads": theory,
                })
                print(f"  exp3 {method:<17} sel={sel:6.1%} k={k:>6}: reads {mr:10.1f} ± {sr:6.1f}  "
                      f"{mm:9.2f} ± {sm:6.2f} ms (teórico {theory})", flush=True)
    info = {"n": n, "P": st.heap.page_count, "h": st.bpt.height, "M": st.seq.main_pages, "leaf_fill": per_leaf,
            "rpp": rpp}
    st.close()
    fields = ["method", "selectivity", "k", "repetitions", "mean_reads", "std_reads", "mean_ms", "std_ms",
              "theoretical_reads"]
    write_csv("exp3_range.csv", fields, ([s[f] for f in fields] for s in summary))
    write_csv("exp3_range_raw.csv", ["method", "selectivity", "low", "high", "reads", "ms"], raw)
    plot(summary, info)
    return summary


def plot(summary: list[dict[str, Any]], info: dict[str, Any]) -> None:
    fig, axes = new_figure(ncols=2, width=5.8)
    colors = {"B+": COLORS["B+"], "Sequential": COLORS["Sequential"], "Full Scan (Heap)": COLORS["Heap"]}
    for method in METHODS:
        data = [s for s in summary if s["method"] == method]
        xs = [s["selectivity"] * 100 for s in data]
        axes[0].errorbar(xs, [s["mean_reads"] for s in data], yerr=[s["std_reads"] for s in data], fmt="o-",
                         color=colors[method], capsize=3, label=f"{method} (medido)")
        axes[0].plot(xs, [s["theoretical_reads"] for s in data], ":", color=colors[method], lw=1.2,
                     label=f"{method} (teórico)")
        axes[1].errorbar(xs, [s["mean_ms"] for s in data], yerr=[s["std_ms"] for s in data], fmt="o-",
                         color=colors[method], capsize=3, label=method)
    # Punto de cruce: el B+ no agrupado cuesta ~k lecturas y el full scan P.
    cross = info["P"] / info["n"] * 100
    for ax in axes:
        ax.axvline(cross, color="gray", ls="--", lw=0.8)
    axes[0].text(cross * 1.08, info["P"] * 0.35, f"cruce teórico\nk ≈ P ({cross:.1f} %)", fontsize=7, color="gray")
    style_axis(axes[0], f"Exp. 3 — Lecturas vs selectividad (N = {info['n']:,})", "selectividad (%)", "lecturas",
               logx=True, logy=True)
    style_axis(axes[1], "Exp. 3 — Tiempo vs selectividad", "selectividad (%)", "ms", logx=True, logy=True)
    axes[0].legend(fontsize=7)
    axes[1].legend(fontsize=8)
    save_figure(fig, "exp3_range.png")


def replot() -> None:
    summary = load_csv("exp3_range.csv")
    full = next(s for s in summary if s["method"] == "Full Scan (Heap)")
    n = round(full["k"] / full["selectivity"])
    plot(summary, {"n": n, "P": full["theoretical_reads"]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true")
    run(quick=parser.parse_args().quick)


if __name__ == "__main__":
    main()
