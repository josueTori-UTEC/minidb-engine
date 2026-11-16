"""Experimento 5: escalabilidad espacial y maldición de la dimensionalidad.

**Parte A — R-Tree vs. recorrido secuencial en 2D.** Para N ∈ {10^4, 5·10^4, 10^5}
puntos geográficos (longitud, latitud) dentro de Lima Metropolitana se miden los
bloques leídos y el tiempo de:

* consultas por radio ``r`` ∈ {0.5, 1, 2, 5} km (``WITHIN_RADIUS``);
* k-NN con k ∈ {5, 20, 50} (``ORDER BY ubicacion <-> (lon, lat) LIMIT k``);

con tres caminos: recorrido secuencial del Heap (sin índice), R-Tree construido
por inserciones (división cuadrática) y R-Tree empaquetado con STR
(``CREATE INDEX`` sobre la tabla ya cargada). Todas las consultas se ejecutan por
SQL, así que el I/O incluye traer los registros de la tabla.

**Parte B — efecto de la dimensión.** Con N vectores uniformes en ``[0, 1]^D`` y
D ∈ {2, 4, 8, 16, 32, 64, 128} se compara el k-NN (k = 10) sobre un R-Tree
(coordenadas float32, páginas de 4 KB) contra el recorrido secuencial de un Heap
con los mismos vectores. Al crecer D caen las entradas por página y las cajas se
solapan: la búsqueda termina visitando casi todas las hojas y pierde contra el
recorrido secuencial.

Uso: ``python -m benchmarks.exp5_spatial [--quick]``
"""

from __future__ import annotations

import argparse
import csv
import heapq
import time
from typing import Any

import numpy as np

from backend.database import Database
from backend.files.heap_file import HeapFile
from backend.indexes.rtree import RTree, SearchTrace
from backend.storage.disk_manager import DiskCounter
from backend.storage.record import Column, ColumnType, Schema
from backend.storage.rid import RID
from benchmarks.common import (
    SEED,
    cleanup,
    fresh_dir,
    load_csv,
    mean_std,
    new_figure,
    no_gc,
    save_figure,
    style_axis,
    write_csv,
)

SIZES = [10_000, 50_000, 100_000]
QUICK_SIZES = [2_000, 5_000]
RADII = [0.5, 1.0, 2.0, 5.0]
KS = [5, 20, 50]
QUERIES = 40
QUICK_QUERIES = 8
LON = (-77.20, -76.80)
LAT = (-12.30, -11.80)

DIMS = [2, 4, 8, 16, 32, 64, 128]
DIM_N = 20_000
QUICK_DIM_N = 2_500
DIM_K = 10
DIM_QUERIES = 30

METHODS = ["Secuencial (Heap)", "R-Tree (inserciones)", "R-Tree (STR)"]
COLORS = {"Secuencial (Heap)": "#4C72B0", "R-Tree (inserciones)": "#DD8452", "R-Tree (STR)": "#55A868",
          "R-Tree": "#55A868"}
FIELDS_2D = ["n", "method", "query", "param", "queries", "reads_mean", "reads_std", "index_reads_mean",
             "table_reads_mean", "ms_mean", "ms_std", "rows_mean", "heap_pages", "index_pages", "height",
             "build_s", "build_reads", "build_writes"]
FIELDS_DIM = ["dims", "n", "k", "leaf_capacity", "internal_capacity", "height", "index_pages", "leaf_pages",
              "heap_pages", "rtree_reads_mean", "rtree_reads_std", "leaves_visited_pct", "rtree_ms_mean",
              "seq_reads", "seq_ms_mean", "build_s"]


# ---------------------------------------------------------------------- parte A
def _points(n: int) -> np.ndarray:
    rng = np.random.default_rng(SEED)
    # 70 % agrupado alrededor de 25 centros (tiendas en distritos) y 30 % uniforme
    centers = np.column_stack([rng.uniform(*LON, 25), rng.uniform(*LAT, 25)])
    clustered = centers[rng.integers(25, size=int(n * 0.7))] + rng.normal(0, 0.012, (int(n * 0.7), 2))
    uniform = np.column_stack([rng.uniform(*LON, n - len(clustered)), rng.uniform(*LAT, n - len(clustered))])
    pts = np.vstack([clustered, uniform])
    rng.shuffle(pts)
    return np.round(pts, 6)


def _write_csv(path, pts: np.ndarray) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for i, (lon, lat) in enumerate(pts):
            writer.writerow([i, f"Local {i}", f"({lon}, {lat})"])


def _run_queries(db: Database, sqls: list[str]) -> dict[str, Any]:
    reads, index_reads, table_reads, ms, rows = [], [], [], [], []
    for sql in sqls:
        with no_gc():
            out = db.execute(sql, 1, 1)
        m = out["metrics"]
        reads.append(m["disk_reads"])
        index_reads.append(m["io"]["index_reads"])
        table_reads.append(m["io"]["table_reads"])
        ms.append(m["exec_ms"])
        rows.append(out["total_rows"])
    r_mean, r_std = mean_std(reads)
    t_mean, t_std = mean_std(ms)
    return {"reads_mean": r_mean, "reads_std": r_std, "index_reads_mean": mean_std(index_reads)[0],
            "table_reads_mean": mean_std(table_reads)[0], "ms_mean": t_mean, "ms_std": t_std,
            "rows_mean": mean_std(rows)[0]}


def _spatial_2d(sizes: list[int], n_queries: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rng = np.random.default_rng(SEED + 1)
    create = "CREATE TABLE locales (id INT PRIMARY KEY, nombre CHAR(30), ubicacion POINT) USING HEAP"
    for n in sizes:
        pts = _points(n)
        centers = pts[rng.integers(n, size=n_queries)] + rng.normal(0, 0.003, (n_queries, 2))
        workload: list[tuple[str, float, list[str]]] = []
        for r in RADII:
            workload.append(("radio", r, [
                f"SELECT id FROM locales WHERE ubicacion WITHIN_RADIUS(({lon:.6f}, {lat:.6f}), {r})"
                for lon, lat in centers
            ]))
        for k in KS:
            workload.append(("knn", k, [
                f"SELECT id FROM locales ORDER BY ubicacion <-> ({lon:.6f}, {lat:.6f}) LIMIT {k}"
                for lon, lat in centers
            ]))
        for method in METHODS:
            name = f"exp5_{n}_{METHODS.index(method)}"
            work = fresh_dir(name)
            _write_csv(work / "locales.csv", pts)
            db = Database(work)
            db.execute(create)
            build = {"build_s": 0.0, "build_reads": 0, "build_writes": 0}
            if method == "R-Tree (inserciones)":
                db.execute("CREATE INDEX idx_geo ON locales (ubicacion) USING RTREE")
                heap_only = _heap_load_cost(n)
                t0 = time.perf_counter()
                m = db.execute("COPY locales FROM 'locales.csv'")["metrics"]
                # costo del índice = carga con índice menos carga del heap solo (1R + 1W por fila)
                build = {"build_s": time.perf_counter() - t0, "build_reads": m["disk_reads"] - heap_only[0],
                         "build_writes": m["disk_writes"] - heap_only[1]}
            else:
                db.execute("COPY locales FROM 'locales.csv'")
                if method == "R-Tree (STR)":
                    t0 = time.perf_counter()
                    m = db.execute("CREATE INDEX idx_geo ON locales (ubicacion) USING RTREE")["metrics"]
                    build = {"build_s": time.perf_counter() - t0, "build_reads": m["disk_reads"],
                             "build_writes": m["disk_writes"]}
            table = db.tables["locales"]
            index = table.indexes.get("idx_geo")
            shape = {"heap_pages": table.file.page_count,
                     "index_pages": index.structure.page_count if index else 0,
                     "height": index.structure.height if index else 0}
            for query, param, sqls in workload:
                stats = _run_queries(db, sqls)
                out.append({"n": n, "method": method, "query": query, "param": param, "queries": len(sqls),
                            **stats, **shape, **build})
                print(f"  N={n:>6} {method:<22} {query:<5} {param:>4}: {stats['reads_mean']:>8.1f} lecturas, "
                      f"{stats['ms_mean']:.2f} ms", flush=True)
            db.close()
            cleanup(name)
    return out


def _heap_load_cost(n: int) -> tuple[int, int]:
    """Lecturas y escrituras de cargar ``n`` filas en el Heap sin índices (se mide una vez)."""
    name = f"exp5_heap_{n}"
    work = fresh_dir(name)
    _write_csv(work / "locales.csv", _points(n))
    db = Database(work)
    db.execute("CREATE TABLE locales (id INT PRIMARY KEY, nombre CHAR(30), ubicacion POINT) USING HEAP")
    m = db.execute("COPY locales FROM 'locales.csv'")["metrics"]
    db.close()
    cleanup(name)
    return m["disk_reads"], m["disk_writes"]


# ---------------------------------------------------------------------- parte B
def _dimension(dims_list: list[int], n: int, n_queries: int) -> list[dict[str, Any]]:
    out = []
    rng = np.random.default_rng(SEED + 2)
    for d in dims_list:
        name = f"exp5_dim_{d}"
        work = fresh_dir(name)
        counter = DiskCounter()
        data = rng.uniform(0, 1, (n, d)).astype(np.float32)
        queries = rng.uniform(0, 1, (n_queries, d)).astype(np.float32)
        schema = Schema([Column("id", ColumnType.INT, primary_key=True), Column("v", ColumnType.VECTOR, d)])
        heap = HeapFile.create(work / "vectores.heap", schema, 4096, counter)
        rids: list[RID] = []
        for i in range(n):
            rids.append(heap.insert((i, tuple(float(x) for x in data[i]))))
        heap.flush()
        tree = RTree.create(work / "vectores.rtr", d, 4096, counter, coord="f")
        t0 = time.perf_counter()
        for i in range(n):
            tree.insert(data[i], None, rids[i])
        build_s = time.perf_counter() - t0
        tree.flush()
        stats = tree.check_invariants()

        rtree_reads, rtree_ms, leaf_pct = [], [], []
        for q in queries:
            counter.reset()
            trace = SearchTrace()
            with no_gc():
                t0 = time.perf_counter()
                hits = tree.knn(q.astype(np.float64), DIM_K, trace=trace)
                for rid, _, _ in hits:  # traer los k registros de la tabla
                    heap.read(rid)
                rtree_ms.append((time.perf_counter() - t0) * 1000)
            rtree_reads.append(counter.disk_reads)
            leaf_pct.append(100.0 * trace.leaves / max(1, stats["leaves"]))

        seq_ms, seq_reads = [], 0
        offset = schema.offsets[1]
        size = 4 * d
        for q in queries[: max(3, n_queries // 5)]:
            counter.reset()
            with no_gc():
                t0 = time.perf_counter()
                best: list[tuple[float, int]] = []  # max-heap de los k mejores (distancia negada)

                def visit(page, q=q, best=best) -> None:
                    buf = page.buf
                    slots = list(page.used_slots())
                    if not slots:
                        return
                    base = page.layout.slots_offset
                    rs = page.layout.record_size
                    block = np.frombuffer(
                        b"".join(bytes(buf[base + s * rs + offset : base + s * rs + offset + size]) for s in slots),
                        dtype=np.float32,
                    ).reshape(len(slots), d)
                    dist = np.sqrt(((block - q) ** 2).sum(axis=1))
                    for j, value in enumerate(dist.tolist()):
                        if len(best) < DIM_K:
                            heapq.heappush(best, (-value, slots[j]))
                        elif value < -best[0][0]:
                            heapq.heapreplace(best, (-value, slots[j]))

                heap.for_each_page(visit)
                seq_ms.append((time.perf_counter() - t0) * 1000)
            seq_reads = counter.disk_reads
        r_mean, r_std = mean_std(rtree_reads)
        row = {
            "dims": d, "n": n, "k": DIM_K, "leaf_capacity": tree.leaf_capacity,
            "internal_capacity": tree.internal_capacity, "height": tree.height, "index_pages": tree.page_count,
            "leaf_pages": stats["leaves"], "heap_pages": heap.page_count, "rtree_reads_mean": r_mean,
            "rtree_reads_std": r_std, "leaves_visited_pct": mean_std(leaf_pct)[0],
            "rtree_ms_mean": mean_std(rtree_ms)[0], "seq_reads": seq_reads, "seq_ms_mean": mean_std(seq_ms)[0],
            "build_s": build_s,
        }
        out.append(row)
        print(f"  D={d:>3}: R-Tree {r_mean:>8.1f} lecturas ({row['leaves_visited_pct']:.0f} % de las hojas) "
              f"vs secuencial {seq_reads} · altura {tree.height}, {tree.leaf_capacity} entradas/hoja", flush=True)
        tree.close()
        heap.close()
        cleanup(name)
    return out


# ---------------------------------------------------------------------- gráficos
def _plot(rows_2d: list[dict[str, Any]], rows_dim: list[dict[str, Any]]) -> None:
    sizes = sorted({r["n"] for r in rows_2d})
    largest = sizes[-1]
    for query, params, xlabel, fname in (("radio", RADII, "radio r (km)", "exp5_radius.png"),
                                         ("knn", KS, "k vecinos", "exp5_knn.png")):
        fig, axes = new_figure(3, width=5.0)
        for method in METHODS:
            pts = [r for r in rows_2d if r["n"] == largest and r["query"] == query and r["method"] == method]
            pts.sort(key=lambda r: r["param"])
            axes[0].plot([r["param"] for r in pts], [r["reads_mean"] for r in pts], "o-", color=COLORS[method],
                         label=method)
            axes[1].plot([r["param"] for r in pts], [r["ms_mean"] for r in pts], "o-", color=COLORS[method],
                         label=method)
        style_axis(axes[0], f"Bloques leídos (N = {largest:,})", xlabel, "lecturas (índice + tabla)", logy=True)
        style_axis(axes[1], f"Tiempo (N = {largest:,})", xlabel, "ms", logy=True)
        mid = params[len(params) // 2]
        for method in METHODS:
            pts = [r for r in rows_2d if r["query"] == query and r["method"] == method and r["param"] == mid]
            pts.sort(key=lambda r: r["n"])
            axes[2].plot([r["n"] for r in pts], [r["reads_mean"] for r in pts], "o-", color=COLORS[method],
                         label=method)
        what = f"r = {mid} km" if query == "radio" else f"k = {mid}"
        style_axis(axes[2], f"Escalabilidad ({what})", "N puntos", "lecturas", logx=True, logy=True)
        axes[0].legend(fontsize=8)
        save_figure(fig, fname)

    if rows_dim:
        fig, axes = new_figure(3, width=5.0)
        dims = [r["dims"] for r in rows_dim]
        axes[0].plot(dims, [r["rtree_reads_mean"] for r in rows_dim], "o-", color=COLORS["R-Tree"], label="R-Tree")
        axes[0].plot(dims, [r["seq_reads"] for r in rows_dim], "s--", color=COLORS["Secuencial (Heap)"],
                     label="Secuencial (Heap)")
        style_axis(axes[0], f"k-NN (k = {DIM_K}, N = {rows_dim[0]['n']:,}): bloques leídos", "dimensión D",
                   "lecturas", logx=True, logy=True)
        axes[0].set_xticks(dims, [str(d) for d in dims])
        axes[0].legend(fontsize=8)
        axes[1].plot(dims, [r["leaves_visited_pct"] for r in rows_dim], "o-", color=COLORS["R-Tree"])
        style_axis(axes[1], "Hojas del R-Tree visitadas", "dimensión D", "% de las hojas", logx=True)
        axes[1].set_xticks(dims, [str(d) for d in dims])
        axes[1].set_ylim(0, 105)
        axes[2].plot(dims, [r["rtree_ms_mean"] for r in rows_dim], "o-", color=COLORS["R-Tree"], label="R-Tree")
        axes[2].plot(dims, [r["seq_ms_mean"] for r in rows_dim], "s--", color=COLORS["Secuencial (Heap)"],
                     label="Secuencial (Heap)")
        style_axis(axes[2], "Tiempo por consulta", "dimensión D", "ms", logx=True, logy=True)
        axes[2].set_xticks(dims, [str(d) for d in dims])
        axes[2].legend(fontsize=8)
        save_figure(fig, "exp5_dimension.png")


def run(quick: bool = False) -> None:
    rows_2d = _spatial_2d(QUICK_SIZES if quick else SIZES, QUICK_QUERIES if quick else QUERIES)
    write_csv("exp5_spatial.csv", FIELDS_2D, [[r[f] for f in FIELDS_2D] for r in rows_2d])
    rows_dim = _dimension(DIMS, QUICK_DIM_N if quick else DIM_N, 10 if quick else DIM_QUERIES)
    write_csv("exp5_dimension.csv", FIELDS_DIM, [[r[f] for f in FIELDS_DIM] for r in rows_dim])
    _plot(rows_2d, rows_dim)


def replot() -> None:
    _plot(load_csv("exp5_spatial.csv"), load_csv("exp5_dimension.csv"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experimento 5: escalabilidad espacial y maldición de la dimensionalidad.")
    parser.add_argument("--quick", action="store_true")
    run(parser.parse_args().quick)
