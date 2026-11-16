"""Consulta híbrida con pre-filtering y post-filtering (tabla del informe, sección del planificador).

Sobre una copia del catálogo con índices B+ (precio) e IVF (embedding) se ejecuta

    SELECT id, precio, embedding <=> q AS d FROM productos WHERE precio <= X ORDER BY d LIMIT 10

con un filtro amplio y uno muy selectivo, forzando cada estrategia con
``SET hybrid.strategy`` y dejando elegir al planificador (``auto``). Se promedian
varias consultas y se guarda ``hybrid_strategies.csv``.

Uso: ``python -m benchmarks.hybrid_strategies [--quick]``
"""

from __future__ import annotations

import argparse

import numpy as np

from benchmarks.catalog import load_features, open_catalog, vector_literal
from benchmarks.common import SEED, cleanup, mean_std, no_gc, write_csv

K = 10
FIELDS = ["filter", "selectivity_pct", "strategy", "plan", "chosen_by_auto", "rows_mean", "reads_mean",
          "index_reads_mean", "table_reads_mean", "ms_mean", "estimated_io_mean", "recall_mean", "queries"]


def run(quick: bool = False) -> None:
    feats = load_features()
    n = len(feats["ids"]) if not quick else 6000
    emb = feats["embeddings"][:n]
    rng = np.random.default_rng(SEED)
    queries = emb[rng.choice(n, size=10 if quick else 40, replace=False)]
    db, _ = open_catalog("hybrid", ("vec",), limit=None if not quick else n)
    db.execute("CREATE INDEX idx_prod_precio ON productos (precio) USING BTREE")
    rows = []
    for label, limit in (("precio <= 300", 300.0), ("precio <= 22", 22.0)):
        total = db.execute(f"SELECT id FROM productos WHERE precio <= {limit}", 1, 1)["total_rows"]
        truth = {}
        db.execute("SET hybrid.strategy = pre")
        for i, q in enumerate(queries):  # el pre-filtering es exacto: sirve de verdad base
            out = db.execute(f"SELECT id, embedding <=> '{vector_literal(q)}' AS d FROM productos "
                             f"WHERE precio <= {limit} ORDER BY d ASC LIMIT {K}", 1, K)
            truth[i] = {r[0] for r in out["rows"]}
        auto_choice = None
        for strategy in ("auto", "pre", "post"):
            db.execute(f"SET hybrid.strategy = {strategy}")
            got, reads, idx, tab, ms, est, recall, plans = [], [], [], [], [], [], [], set()
            for i, q in enumerate(queries):
                with no_gc():
                    out = db.execute(f"SELECT id, embedding <=> '{vector_literal(q)}' AS d FROM productos "
                                     f"WHERE precio <= {limit} ORDER BY d ASC LIMIT {K}", 1, K)
                m = out["metrics"]
                got.append(out["total_rows"])
                reads.append(m["disk_reads"])
                idx.append(m["io"]["index_reads"])
                tab.append(m["io"]["table_reads"])
                ms.append(m["exec_ms"])
                est.append(out["plan"]["estimated_io"])
                plans.add(f"{out['plan']['type']} ({out['plan']['strategy']})")
                recall.append(len(truth[i] & {r[0] for r in out["rows"]}) / max(1, len(truth[i])))
            plan = " / ".join(sorted(plans))
            if strategy == "auto":
                auto_choice = plan
                continue
            rows.append([label, 100.0 * total / n, strategy, plan, "sí" if plan == auto_choice else "no",
                         mean_std(got)[0], mean_std(reads)[0], mean_std(idx)[0], mean_std(tab)[0], mean_std(ms)[0],
                         mean_std(est)[0], mean_std(recall)[0], len(queries)])
            print(f"  {label:<14} {strategy:<5} {plan:<34} filas={rows[-1][5]:.1f} lecturas={rows[-1][6]:.0f} "
                  f"ms={rows[-1][9]:.1f} recall={rows[-1][11]:.2f} auto={rows[-1][4]}", flush=True)
    db.close()
    cleanup("hybrid")
    write_csv("hybrid_strategies.csv", FIELDS, rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Consulta híbrida: pre-filtering vs. post-filtering.")
    parser.add_argument("--quick", action="store_true")
    run(parser.parse_args().quick)
