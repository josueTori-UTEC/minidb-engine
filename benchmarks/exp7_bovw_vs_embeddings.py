"""Experimento 7: paradigma clásico (BoVW) vs. paradigma moderno (embeddings densos).

Sobre el catálogo de moda (44 072 imágenes de 60 × 80) se toma un conjunto de
imágenes de consulta de las categorías de artículo más frecuentes (20 por
categoría) y se recuperan sus 10 vecinos con cada representación, por SQL sobre la
misma tabla:

* **BoVW**: SIFT + *codebook* K-Means de 1 000 palabras visuales; el histograma de
  cada imagen se indexa con el índice invertido (TF-IDF + coseno).
* **Embedding + IVF**: ResNet-18 (512 D) -> PCA 128 D, índice IVF (coseno).
* **Embedding exacto**: los mismos vectores con k-NN por recorrido secuencial.

Un resultado es relevante si es del mismo tipo de artículo que la consulta (la
propia imagen se excluye). Se reporta Precision@5 y Precision@10 (global y por
categoría), el tiempo de extracción de características por imagen y la latencia y
los bloques leídos por consulta.

Uso: ``python -m benchmarks.exp7_bovw_vs_embeddings [--quick]``
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from typing import Any

import numpy as np

from benchmarks.catalog import DATA, load_features, open_catalog, vector_literal
from benchmarks.common import SEED, cleanup, load_csv, mean_std, new_figure, no_gc, save_figure, style_axis, write_csv

CATEGORIES = 15
PER_CATEGORY = 20
QUICK_PER_CATEGORY = 3
EXACT_QUERIES = 20
K = 10
METHODS = ["BoVW (SIFT + índice invertido)", "Embedding (ResNet-18 + IVF)", "Embedding (k-NN exacto)"]
COLORS = {METHODS[0]: "#DD8452", METHODS[1]: "#55A868", METHODS[2]: "#4C72B0"}
PRECISION_FIELDS = ["method", "scope", "queries", "p_at_5", "p_at_10"]
COST_FIELDS = ["method", "extraction_ms", "extraction_note", "query_ms_mean", "query_ms_std", "reads_mean",
               "index_reads_mean", "table_reads_mean", "index_mb", "index_build_s", "representation"]


def _precision(labels: list[str], truth: str) -> tuple[float, float]:
    hits = [1.0 if label == truth else 0.0 for label in labels[:K]]
    hits += [0.0] * (K - len(hits))
    return sum(hits[:5]) / 5, sum(hits) / K


def _extraction_ms(samples: int) -> dict[str, float]:
    """Tiempo de extracción por imagen medido una a una (como en una consulta de la aplicación)."""
    from backend.ml import features as F

    paths = sorted((DATA / "media").glob("*.jpg"))[:samples]
    codebook = np.load(DATA / "models" / F.CODEBOOK_FILE)["codebook"]
    images = [F.load_image(p.read_bytes()) for p in paths]
    F.resnet_features(images[:1])  # carga del modelo fuera de la medición
    F.sift_descriptors(images[0])
    t0 = time.perf_counter()
    for image in images:
        F.resnet_features([image])
    resnet = (time.perf_counter() - t0) * 1000 / len(images)
    t0 = time.perf_counter()
    for image in images:
        F.words_text(F.quantize(F.sift_descriptors(image), codebook))
    sift = (time.perf_counter() - t0) * 1000 / len(images)
    return {"resnet": resnet, "sift": sift}


def run(quick: bool = False) -> None:
    feats = load_features()
    ids = feats["ids"]
    tipo = feats["tipo"]
    emb = feats["embeddings"]
    n = len(ids) if not quick else 6000
    ids, tipo, emb = ids[:n], tipo[:n], emb[:n]
    label_of = {int(i): str(t) for i, t in zip(ids, tipo)}
    rng = np.random.default_rng(SEED)
    top = [name for name, _ in Counter(tipo.tolist()).most_common(CATEGORIES)]
    per = QUICK_PER_CATEGORY if quick else PER_CATEGORY
    queries: list[int] = []
    for name in top:
        pool = np.flatnonzero(tipo == name)
        queries.extend(int(i) for i in rng.choice(pool, size=min(per, len(pool)), replace=False))

    db, build = open_catalog("exp7", ("id", "bovw", "vec"), limit=None if not quick else n)
    table = db.tables["productos"]
    sizes = {name: idx.size_bytes() / 2**20 for name, idx in table.indexes.items()}
    results: dict[str, list[tuple[str, float, float]]] = {m: [] for m in METHODS}
    cost: dict[str, dict[str, list[float]]] = {m: {"ms": [], "reads": [], "index": [], "table": []} for m in METHODS}

    def record(method: str, out: dict[str, Any], truth: str, own_id: int) -> None:
        labels = [row[1] for row in out["rows"] if row[0] != own_id]
        results[method].append((truth, *_precision(labels, truth)))
        m = out["metrics"]
        cost[method]["ms"].append(m["exec_ms"])
        cost[method]["reads"].append(m["disk_reads"])
        cost[method]["index"].append(m["io"]["index_reads"])
        cost[method]["table"].append(m["io"]["table_reads"])

    # precisión del k-NN exacto con numpy (misma métrica); su costo se mide por SQL en un subconjunto
    norm = emb / np.linalg.norm(emb, axis=1, keepdims=True)
    for count, i in enumerate(queries):
        pid = int(ids[i])
        truth = str(tipo[i])
        row = db.execute(f"SELECT palabras FROM productos WHERE id = {pid}")["rows"][0]
        words = row[0]
        vector = vector_literal(emb[i])
        with no_gc():
            if words:
                out = db.execute(
                    f"SELECT id, tipo, COSINE_SCORE() AS s FROM productos WHERE palabras MATCH '{words}' "
                    f"ORDER BY s DESC LIMIT {K + 1}", 1, K + 1)
                record(METHODS[0], out, truth, pid)
            else:  # imagen sin puntos de interés: BoVW no puede recuperar nada
                results[METHODS[0]].append((truth, 0.0, 0.0))
            out = db.execute(
                f"SELECT id, tipo, embedding <=> '{vector}' AS d FROM productos ORDER BY d ASC LIMIT {K + 1}", 1, K + 1)
            record(METHODS[1], out, truth, pid)
            if count < EXACT_QUERIES:
                out = db.execute(
                    f"SELECT id, tipo, embedding <-> '{vector}' AS d FROM productos ORDER BY d ASC LIMIT {K + 1}",
                    1, K + 1)
                m = out["metrics"]
                cost[METHODS[2]]["ms"].append(m["exec_ms"])
                cost[METHODS[2]]["reads"].append(m["disk_reads"])
                cost[METHODS[2]]["index"].append(0)
                cost[METHODS[2]]["table"].append(m["io"]["table_reads"])
        order = np.argsort(-(norm @ norm[i]), kind="stable")[: K + 1]
        labels = [label_of[int(ids[j])] for j in order if j != i]
        results[METHODS[2]].append((truth, *_precision(labels, truth)))
        if (count + 1) % 50 == 0:
            print(f"  {count + 1}/{len(queries)} consultas", flush=True)
    db.close()
    cleanup("exp7")

    precision_rows = []
    for method in METHODS:
        items = results[method]
        precision_rows.append([method, "global", len(items), mean_std([p for _, p, _ in items])[0],
                               mean_std([p for _, _, p in items])[0]])
        for name in top:
            sub = [(p5, p10) for t, p5, p10 in items if t == name]
            if sub:
                precision_rows.append([method, name, len(sub), mean_std([a for a, _ in sub])[0],
                                       mean_std([b for _, b in sub])[0]])
    write_csv("exp7_precision.csv", PRECISION_FIELDS, precision_rows)

    extraction = _extraction_ms(20 if quick else 100)
    timing = feats["timing"]  # totales de la corrida por lotes: ResNet, SIFT, K-Means, cuantización
    total = len(feats["ids"])
    batch_resnet = float(timing[0]) * 1000 / total
    batch_sift = float(timing[1] + timing[3]) * 1000 / total
    cost_rows = []
    for method, ext, note, index_name, rep in (
        (METHODS[0], extraction["sift"], f"por lotes: {batch_sift:.2f} ms/imagen; codebook K-Means: {float(timing[2]):.0f} s",
         "idx_prod_bovw", "histograma disperso (≤ 100 de 1000 palabras)"),
        (METHODS[1], extraction["resnet"], f"por lotes de 256 (GPU): {batch_resnet:.2f} ms/imagen",
         "idx_prod_vec", "128 float32 (512 bytes)"),
        (METHODS[2], extraction["resnet"], f"por lotes de 256 (GPU): {batch_resnet:.2f} ms/imagen", None,
         "128 float32 (512 bytes)"),
    ):
        c = cost[method]
        ms_mean, ms_std = mean_std(c["ms"])
        cost_rows.append([
            method, ext, note, ms_mean, ms_std, mean_std(c["reads"])[0], mean_std(c["index"])[0],
            mean_std(c["table"])[0], sizes.get(index_name, 0.0) if index_name else 0.0,
            build.get("bovw" if index_name == "idx_prod_bovw" else "vec", 0.0) if index_name else 0.0, rep,
        ])
    write_csv("exp7_cost.csv", COST_FIELDS, cost_rows)
    for row in precision_rows:
        if row[1] == "global":
            print(f"  {row[0]:<34} P@5 = {row[3]:.3f}  P@10 = {row[4]:.3f}", flush=True)
    replot()


def replot() -> None:
    precision = load_csv("exp7_precision.csv")
    cost = load_csv("exp7_cost.csv")
    fig, axes = new_figure(2, width=7.0, height=4.4)
    overall = {r["method"]: r for r in precision if r["scope"] == "global"}
    width = 0.36
    xs = range(len(METHODS))
    axes[0].bar([x - width / 2 for x in xs], [overall[m]["p_at_5"] for m in METHODS], width, label="Precision@5",
                color=[COLORS[m] for m in METHODS])
    axes[0].bar([x + width / 2 for x in xs], [overall[m]["p_at_10"] for m in METHODS], width, label="Precision@10",
                color=[COLORS[m] for m in METHODS], alpha=0.55)
    for x, m in zip(xs, METHODS):
        axes[0].text(x - width / 2, overall[m]["p_at_5"] + 0.01, f"{overall[m]['p_at_5']:.2f}", ha="center", fontsize=8)
        axes[0].text(x + width / 2, overall[m]["p_at_10"] + 0.01, f"{overall[m]['p_at_10']:.2f}", ha="center", fontsize=8)
    style_axis(axes[0], f"Precisión global ({overall[METHODS[0]]['queries']} consultas): P@5 (sólido) y P@10 (claro)",
               "", "precisión")
    axes[0].set_xticks(list(xs), ["BoVW\n(SIFT + invertido)", "Embedding\n(ResNet-18 + IVF)", "Embedding\n(k-NN exacto)"],
                       fontsize=8)
    axes[0].set_ylim(0, 1.05)
    scopes = [r["scope"] for r in precision if r["method"] == METHODS[0] and r["scope"] != "global"]
    for j, method in enumerate(METHODS[:2]):
        by = {r["scope"]: r["p_at_10"] for r in precision if r["method"] == method}
        axes[1].barh([i + (j - 0.5) * 0.4 for i in range(len(scopes))], [by.get(s, 0) for s in scopes], 0.4,
                     color=COLORS[method], label=method)
    style_axis(axes[1], "Precision@10 por tipo de artículo", "precisión", "")
    axes[1].set_yticks(range(len(scopes)), scopes, fontsize=7)
    axes[1].invert_yaxis()
    axes[1].set_xlim(0, 1.05)
    axes[1].legend(fontsize=7, loc="lower right")
    save_figure(fig, "exp7_precision.png")

    fig, axes = new_figure(3, width=4.6)
    names = ["BoVW", "Emb. + IVF", "Emb. exacto"]
    colors = [COLORS[m] for m in METHODS]
    by_method = {r["method"]: r for r in cost}
    for ax, field, title, ylabel, log in (
        (axes[0], "extraction_ms", "Extracción de características", "ms por imagen", False),
        (axes[1], "query_ms_mean", "Latencia de la consulta", "ms", True),
        (axes[2], "reads_mean", "Bloques leídos por consulta", "lecturas", True),
    ):
        values = [by_method[m][field] or 0 for m in METHODS]
        ax.bar(names, values, color=colors)
        for x, v in enumerate(values):
            ax.text(x, v, f"{v:,.1f}", ha="center", va="bottom", fontsize=8)
        style_axis(ax, title, "", ylabel, logy=log)
        ax.tick_params(axis="x", labelsize=8)
    save_figure(fig, "exp7_cost.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experimento 7: BoVW vs. embeddings densos.")
    parser.add_argument("--quick", action="store_true")
    run(parser.parse_args().quick)
