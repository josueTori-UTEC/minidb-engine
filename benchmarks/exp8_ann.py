"""Experimento 8: compromiso eficiencia vs. calidad en la búsqueda aproximada (IVF).

Sobre los 44 072 embeddings de 128 dimensiones del catálogo se ejecutan consultas
k-NN (k = 10) por SQL (``ORDER BY embedding <=> q LIMIT 10``). La verdad base es el
k-NN lineal exacto. Se varía ``ivf.probes`` ∈ {1, 2, 5, 10, 20, 50, 100, C} y, además,
el número de celdas del índice (``lists`` = √N/4, √N y 4·√N) y se mide:

* **Recall@10** respecto del k-NN exacto;
* la latencia media (ms);
* los bloques físicos leídos (directorio de centroides + páginas de las listas
  sondeadas + los 10 registros traídos de la tabla).

Como referencia se mide el recorrido secuencial exacto por SQL (``<->``), que lee
todas las páginas de la tabla.

Uso: ``python -m benchmarks.exp8_ann [--quick]``
"""

from __future__ import annotations

import argparse
import math
from typing import Any

import numpy as np

from benchmarks.catalog import load_features, open_catalog, vector_literal
from benchmarks.common import SEED, cleanup, load_csv, mean_std, new_figure, no_gc, save_figure, style_axis, write_csv

K = 10
QUERIES = 200
QUICK_QUERIES = 25
EXACT_QUERIES = 15
PROBES = [1, 2, 5, 10, 20, 50, 100]
FIELDS = ["lists", "probes", "queries", "recall_mean", "recall_std", "ms_mean", "ms_std", "reads_mean",
          "index_reads_mean", "table_reads_mean", "scanned_mean", "scanned_pct", "dir_pages", "list_pages",
          "build_s", "n"]


def run(quick: bool = False) -> None:
    feats = load_features()
    n = len(feats["ids"]) if not quick else 6000
    ids = feats["ids"][:n]
    emb = feats["embeddings"][:n].astype(np.float32)
    rng = np.random.default_rng(SEED)
    n_queries = QUICK_QUERIES if quick else QUERIES
    # consultas: vectores del catálogo con un poco de ruido (no coinciden con ningún producto)
    queries = emb[rng.choice(n, size=n_queries, replace=False)] + rng.normal(0, 0.03, (n_queries, emb.shape[1]))
    queries = (queries / np.linalg.norm(queries, axis=1, keepdims=True)).astype(np.float32)
    norm = emb / np.linalg.norm(emb, axis=1, keepdims=True)
    truth = [set(ids[np.argsort(-(norm @ q), kind="stable")[:K]].tolist()) for q in queries]

    base = max(1, int(math.sqrt(n)))
    list_options = sorted({max(2, base // 4), base, base * 4})
    db, _ = open_catalog("exp8", (), limit=None if not quick else n)
    rows: list[dict[str, Any]] = []

    # verdad base por SQL: recorrido secuencial exacto
    ms, reads = [], []
    for q in queries[:EXACT_QUERIES]:
        with no_gc():
            out = db.execute(
                f"SELECT id, embedding <-> '{vector_literal(q)}' AS d FROM productos ORDER BY d ASC LIMIT {K}", 1, K)
        ms.append(out["metrics"]["exec_ms"])
        reads.append(out["metrics"]["disk_reads"])
    rows.append({"lists": 0, "probes": 0, "queries": len(ms), "recall_mean": 1.0, "recall_std": 0.0,
                 "ms_mean": mean_std(ms)[0], "ms_std": mean_std(ms)[1], "reads_mean": mean_std(reads)[0],
                 "index_reads_mean": 0.0, "table_reads_mean": mean_std(reads)[0], "scanned_mean": float(n),
                 "scanned_pct": 100.0, "dir_pages": 0, "list_pages": 0, "build_s": 0.0, "n": n})
    print(f"  k-NN exacto (secuencial): {rows[0]['reads_mean']:.0f} lecturas, {rows[0]['ms_mean']:.1f} ms", flush=True)

    for lists in list_options:
        out = db.execute(
            f"CREATE INDEX idx_prod_vec ON productos (embedding) USING IVF WITH (metric = 'COSINE', lists = {lists})")
        build_s = out["metrics"]["exec_ms"] / 1000
        s = db.tables["productos"].indexes["idx_prod_vec"].structure.stats()
        for probes in [p for p in PROBES if p < lists] + [lists]:
            db.execute(f"SET ivf.probes = {probes}")
            recall, ms, reads, idx_reads, tab_reads, scanned = [], [], [], [], [], []
            for q, expected in zip(queries, truth):
                with no_gc():
                    res = db.execute(
                        f"SELECT id, embedding <=> '{vector_literal(q)}' AS d FROM productos ORDER BY d ASC LIMIT {K}",
                        1, K)
                recall.append(len(expected & {row[0] for row in res["rows"]}) / K)
                m = res["metrics"]
                ms.append(m["exec_ms"])
                reads.append(m["disk_reads"])
                idx_reads.append(m["io"]["index_reads"])
                tab_reads.append(m["io"]["table_reads"])
                scanned.append(res["plan"]["vector"]["scanned"])
            r_mean, r_std = mean_std(recall)
            t_mean, t_std = mean_std(ms)
            rows.append({"lists": lists, "probes": probes, "queries": n_queries, "recall_mean": r_mean,
                         "recall_std": r_std, "ms_mean": t_mean, "ms_std": t_std, "reads_mean": mean_std(reads)[0],
                         "index_reads_mean": mean_std(idx_reads)[0], "table_reads_mean": mean_std(tab_reads)[0],
                         "scanned_mean": mean_std(scanned)[0], "scanned_pct": 100.0 * mean_std(scanned)[0] / n,
                         "dir_pages": s["dir_pages"] - 1, "list_pages": s["list_pages"] - 1, "build_s": build_s, "n": n})
            print(f"  lists={lists:>4} probes={probes:>4}: Recall@10 = {r_mean:.3f}, {t_mean:.2f} ms, "
                  f"{rows[-1]['reads_mean']:.0f} lecturas ({rows[-1]['scanned_pct']:.1f} % de los vectores)", flush=True)
        db.execute("DROP INDEX idx_prod_vec")
    db.close()
    cleanup("exp8")
    write_csv("exp8_ann.csv", FIELDS, [[r[f] for f in FIELDS] for r in rows])
    _plot(rows)


def _plot(rows: list[dict[str, Any]]) -> None:
    exact = next(r for r in rows if r["lists"] == 0)
    options = sorted({r["lists"] for r in rows if r["lists"]})
    palette = ["#DD8452", "#55A868", "#8172B3", "#C44E52"]
    fig, axes = new_figure(3, width=5.0)
    for color, lists in zip(palette, options):
        pts = sorted((r for r in rows if r["lists"] == lists), key=lambda r: r["probes"])
        label = f"lists = {lists}"
        axes[0].plot([r["probes"] for r in pts], [100 * r["recall_mean"] for r in pts], "o-", color=color, label=label)
        axes[1].plot([r["ms_mean"] for r in pts], [100 * r["recall_mean"] for r in pts], "o-", color=color, label=label)
        axes[2].plot([r["reads_mean"] for r in pts], [100 * r["recall_mean"] for r in pts], "o-", color=color, label=label)
    axes[1].axvline(exact["ms_mean"], color="#4C72B0", linestyle="--", label="k-NN exacto (secuencial)")
    axes[2].axvline(exact["reads_mean"], color="#4C72B0", linestyle="--", label="k-NN exacto (secuencial)")
    style_axis(axes[0], f"Recall@10 vs. probes (N = {exact['n']:,})", "probes (celdas sondeadas)", "Recall@10 (%)", logx=True)
    style_axis(axes[1], "Recall@10 vs. latencia", "ms por consulta", "Recall@10 (%)", logx=True)
    style_axis(axes[2], "Recall@10 vs. bloques leídos", "lecturas de disco por consulta", "Recall@10 (%)", logx=True)
    for ax in axes:
        ax.set_ylim(0, 103)
    axes[0].legend(fontsize=8)
    axes[1].legend(fontsize=7, loc="lower right")
    save_figure(fig, "exp8_recall.png")


def replot() -> None:
    _plot(load_csv("exp8_ann.csv"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experimento 8: Recall@10 vs. latencia e I/O en el índice IVF.")
    parser.add_argument("--quick", action="store_true")
    run(parser.parse_args().quick)
