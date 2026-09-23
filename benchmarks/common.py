"""Utilidades compartidas por los experimentos.

Cada experimento trabaja sobre archivos nuevos en ``benchmarks/work/`` (no se
versiona), usa datos generados con semilla fija y guarda sus resultados (CSV +
PNG) en ``benchmarks/results/``. matplotlib solo se usa aquí, nunca en el motor.
"""

from __future__ import annotations

import csv
import gc
import importlib.util
import math
import os
import platform
import shutil
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from backend.storage.disk_manager import DiskCounter  # noqa: E402
from backend.storage.record import Column, ColumnType, Schema  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = Path(os.environ.get("MINIDB_BENCH_WORK", ROOT / "benchmarks" / "work"))
RESULTS_DIR = Path(os.environ.get("MINIDB_BENCH_RESULTS", ROOT / "benchmarks" / "results"))
SEED = 42

EMPLEADOS = Schema(
    [
        Column("id", ColumnType.INT, primary_key=True),
        Column("nombre", ColumnType.CHAR, 30),
        Column("dept", ColumnType.CHAR, 20),
        Column("salario", ColumnType.FLOAT),
    ]
)
ID_COLUMN = EMPLEADOS.columns[0]

# Paleta consistente entre gráficos (una por estructura).
COLORS = {
    "Heap": "#4C72B0",
    "Sequential sin reorg": "#DD8452",
    "Sequential con reorg": "#C44E52",
    "Sequential": "#C44E52",
    "B+": "#55A868",
    "Hash": "#8172B3",
    "Full Scan (Heap)": "#4C72B0",
    "Búsqueda binaria (Sequential)": "#C44E52",
    "B+ (IndexScan)": "#55A868",
    "Hash (IndexScan)": "#8172B3",
}


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("minidb_generate", ROOT / "data" / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GENERATOR = _load_generator()


def rows(n: int, seed: int = SEED) -> Iterator[tuple[int, str, str, float]]:
    """Filas del dataset sintético (mismo generador que ``data/generate.py``)."""
    return _GENERATOR.generate_rows(n, seed)


def fresh_dir(name: str) -> Path:
    path = WORK_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def cleanup(name: str) -> None:
    shutil.rmtree(WORK_DIR / name, ignore_errors=True)


@contextmanager
def no_gc() -> Iterator[None]:
    """Desactiva el recolector cíclico durante una medición (menos ruido en ms)."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


class Measure:
    """Mide I/O (con un DiskCounter) y tiempo de un bloque de código."""

    def __init__(self, counter: DiskCounter) -> None:
        self.counter = counter
        self.reads = self.writes = 0
        self.seconds = 0.0

    def __enter__(self) -> Measure:
        self._r0, self._w0 = self.counter.snapshot()
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self._t0
        r1, w1 = self.counter.snapshot()
        self.reads, self.writes = r1 - self._r0, w1 - self._w0


def mean_std(values: Sequence[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return float(values[0]), 0.0
    return statistics.fmean(values), statistics.stdev(values)


def write_csv(name: str, header: Sequence[str], data: Iterable[Sequence[Any]]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in data:
            writer.writerow([_fmt(v) for v in row])
    return path


def _fmt(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.6g}" if abs(value) >= 1e-3 or value == 0 else f"{value:.3e}"
    return value


def save_figure(fig: Any, name: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def new_figure(ncols: int = 1, width: float = 6.4, height: float = 4.2) -> tuple[Any, Any]:
    fig, axes = plt.subplots(1, ncols, figsize=(width * ncols, height))
    return fig, axes


def style_axis(ax: Any, title: str, xlabel: str, ylabel: str, *, logx: bool = False, logy: bool = False) -> None:
    ax.set_title(title, fontsize=11)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if logx:
        ax.set_xscale("log")
    if logy:
        ax.set_yscale("log")
    ax.grid(True, which="both", alpha=0.25)


def log2_ceil(x: float) -> int:
    return max(1, math.ceil(math.log2(x))) if x > 1 else 1


def environment() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "cpus": str(os.cpu_count()),
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0
