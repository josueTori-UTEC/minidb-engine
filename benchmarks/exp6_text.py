"""Experimento 6: índice invertido en disco vs. full scan e índice GIN de PostgreSQL.

Corpus: las descripciones del catálogo de productos (``data/productos.csv``, en
inglés), escalando de 1 000 a todos los documentos (≥ 20 000).

**Construcción.** Para cada tamaño se crea la tabla ``docs(id, cuerpo)`` y se mide
el tiempo de ``CREATE INDEX ... USING INVERTED`` y el tamaño en disco del índice
(``vocab.dic`` + ``postings.inv`` + ``docs.bin``), junto al tiempo y tamaño del
índice ``GIN (to_tsvector('english', cuerpo))`` de PostgreSQL sobre los mismos
documentos.

**Consultas.** Con el corpus completo, consultas de 1, 3 y 5 términos (tomados de
documentos al azar para que tengan resultados) de cuatro tipos: ``AND``, ``OR``,
``AND NOT`` y ranking Top-10. Se comparan:

* **MiniDB (índice invertido)**: latencia y bloques leídos (índice y tabla);
* **MiniDB (full scan)**: la misma consulta sin índice (se tokeniza cada documento;
  para el ranking hay que calcular df, pesos y normas de todo el corpus en memoria);
* **PostgreSQL (GIN)**: ``to_tsquery`` + ``ts_rank``, con el tiempo de ejecución y los
  *buffers* de ``EXPLAIN (ANALYZE, BUFFERS)``. PostgreSQL usa páginas de 8 KB y
  *buffer pool*; MiniDB páginas de 4 KB sin caché propia.

Además se mide la latencia del Top-10 de 3 términos para cada tamaño de corpus.

PostgreSQL es opcional: si no hay conexión (``MINIDB_PG_DSN``, por defecto la base
del usuario local) se omiten sus filas. Se usa una base temporal ``minidb_bench``
que se elimina al terminar.

Uso: ``python -m benchmarks.exp6_text [--quick]``
"""

from __future__ import annotations

import argparse
import csv
import heapq
import math
import os
import random
import re
import time
from collections import Counter
from typing import Any

from backend.database import Database
from backend.text.analyzer import Analyzer, stopwords
from benchmarks.common import (
    ROOT,
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

SIZES = [1_000, 2_000, 5_000, 10_000, 20_000, 44_072]
QUICK_SIZES = [500, 1_000, 2_000]
TERMS = [1, 3, 5]
KINDS = ["AND", "OR", "AND NOT", "Top-10"]
QUERIES = 30
QUICK_QUERIES = 6
SCAN_QUERIES = 3
TOP_K = 10
PG_DATABASE = "minidb_bench"

ENGINES = ["MiniDB (índice invertido)", "MiniDB (full scan)", "PostgreSQL (GIN)"]
COLORS = {ENGINES[0]: "#55A868", ENGINES[1]: "#4C72B0", ENGINES[2]: "#C44E52"}
BUILD_FIELDS = ["n", "engine", "build_s", "size_bytes", "terms", "postings", "vocab_pages", "postings_pages",
                "docs_pages", "heap_pages", "build_reads", "build_writes", "runs"]
QUERY_FIELDS = ["n", "engine", "kind", "terms", "queries", "ms_mean", "ms_std", "reads_mean", "index_reads_mean",
                "table_reads_mean", "rows_mean"]
_WORD = re.compile(r"[a-z]{3,}")


# ---------------------------------------------------------------------- datos y consultas
def load_corpus(limit: int) -> list[tuple[int, str]]:
    path = ROOT / "data" / "productos.csv"
    if not path.exists():
        raise SystemExit("falta data/productos.csv: ejecute primero `python data/prepare_fashion.py`")
    docs = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        col = header.index("descripcion")
        for row in reader:
            docs.append((int(row[0]), row[col]))
            if len(docs) >= limit:
                break
    return docs


def make_queries(docs: list[tuple[int, str]], per_config: int) -> dict[tuple[str, int], list[list[str]]]:
    """Para cada (tipo, nº de términos), listas de palabras tomadas de un mismo documento."""
    rng = random.Random(SEED)
    stop = stopwords("english")
    out: dict[tuple[str, int], list[list[str]]] = {}
    for kind in KINDS:
        for t in TERMS:
            need = t + 1 if kind == "AND NOT" else t
            queries: list[list[str]] = []
            while len(queries) < per_config:
                words = list(dict.fromkeys(w for w in _WORD.findall(rng.choice(docs)[1].lower()) if w not in stop))
                if len(words) < need:
                    continue
                chosen = rng.sample(words, t)
                if kind == "AND NOT":
                    # el término negado sale de otro documento (así la consulta sigue teniendo resultados)
                    other = [w for w in _WORD.findall(rng.choice(docs)[1].lower()) if w not in stop and w not in chosen]
                    if not other:
                        continue
                    chosen.append(rng.choice(other))
                queries.append(chosen)
            out[(kind, t)] = queries
    return out


def minidb_sql(kind: str, words: list[str]) -> str:
    if kind == "Top-10":
        return (f"SELECT id, COSINE_SCORE() AS r FROM docs WHERE cuerpo MATCH '{' '.join(words)}' "
                f"ORDER BY r DESC LIMIT {TOP_K}")
    if kind == "AND":
        expr = " AND ".join(words)
    elif kind == "OR":
        expr = " OR ".join(words)
    else:
        expr = " AND ".join(words[:-1]) + f" AND NOT {words[-1]}"
    return f"SELECT id FROM docs WHERE cuerpo MATCH '{expr}'"


def pg_sql(kind: str, words: list[str]) -> str:
    vector = "to_tsvector('english', cuerpo)"
    if kind == "Top-10":
        q = " | ".join(words)
        return (f"SELECT id, ts_rank({vector}, q) AS r FROM docs, to_tsquery('english', '{q}') q "
                f"WHERE {vector} @@ q ORDER BY r DESC LIMIT {TOP_K}")
    if kind == "AND":
        q = " & ".join(words)
    elif kind == "OR":
        q = " | ".join(words)
    else:
        q = " & ".join(words[:-1]) + f" & !{words[-1]}"
    return f"SELECT id FROM docs WHERE {vector} @@ to_tsquery('english', '{q}')"


# ---------------------------------------------------------------------- MiniDB
def scan_top_k(db: Database, words: list[str], analyzer: Analyzer) -> tuple[int, int]:
    """Ranking TF-IDF + coseno sin índice: un recorrido de la tabla y todo el modelo en memoria."""
    table = db.tables["docs"]
    tfs: list[Counter[str]] = []
    ids: list[int] = []
    df: Counter[str] = Counter()
    for _, values in table.scan():  # P lecturas
        tf = analyzer.term_frequencies(values[1])
        tfs.append(tf)
        ids.append(values[0])
        df.update(tf.keys())
    n = len(tfs)
    qtf = Counter(analyzer.tokens(" ".join(words)))
    idf = {t: math.log10(n / df[t]) for t in qtf if df.get(t)}
    q_norm = math.sqrt(sum(((1 + math.log10(c)) * idf[t]) ** 2 for t, c in qtf.items() if t in idf))
    scored = []
    for i, tf in enumerate(tfs):
        dot = sum((1 + math.log10(c)) * idf[t] * (1 + math.log10(tf[t])) * idf[t] for t, c in qtf.items()
                  if t in idf and t in tf)
        if dot:
            norm = math.sqrt(sum(((1 + math.log10(c)) * math.log10(n / df[t])) ** 2 for t, c in tf.items()))
            scored.append((dot / (norm * q_norm) if norm and q_norm else 0.0, ids[i]))
    return len(heapq.nlargest(TOP_K, scored)), len(scored)


def run_minidb(db: Database, queries: dict[tuple[str, int], list[list[str]]], n: int, engine: str,
               only: set[tuple[str, int]] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    out = []
    analyzer = Analyzer("english")
    scan = engine == ENGINES[1]
    for (kind, t), items in queries.items():
        if only is not None and (kind, t) not in only:
            continue
        items = items[:limit] if limit else items
        ms, reads, idx_reads, tab_reads, rows = [], [], [], [], []
        for words in items:
            if scan and kind == "Top-10":
                db.counter.reset()
                with no_gc():
                    t0 = time.perf_counter()
                    _, matched = scan_top_k(db, words, analyzer)
                    ms.append((time.perf_counter() - t0) * 1000)
                reads.append(db.counter.disk_reads)
                idx_reads.append(0)
                tab_reads.append(db.counter.disk_reads)
                rows.append(min(TOP_K, matched))
                continue
            with no_gc():
                res = db.execute(minidb_sql(kind, words), 1, 1)
            m = res["metrics"]
            ms.append(m["exec_ms"])
            reads.append(m["disk_reads"])
            idx_reads.append(m["io"]["index_reads"])
            tab_reads.append(m["io"]["table_reads"])
            rows.append(res["total_rows"])
        mean, std = mean_std(ms)
        out.append({"n": n, "engine": engine, "kind": kind, "terms": t, "queries": len(items), "ms_mean": mean,
                    "ms_std": std, "reads_mean": mean_std(reads)[0], "index_reads_mean": mean_std(idx_reads)[0],
                    "table_reads_mean": mean_std(tab_reads)[0], "rows_mean": mean_std(rows)[0]})
    return out


# ---------------------------------------------------------------------- PostgreSQL
def pg_connect():
    """Conexión a la base temporal del experimento, o ``None`` si PostgreSQL no está disponible."""
    try:
        import psycopg
    except ImportError:
        print("  (psycopg no está instalado: se omite PostgreSQL)")
        return None
    candidates = [os.environ["MINIDB_PG_DSN"]] if os.environ.get("MINIDB_PG_DSN") else [
        f"dbname={os.environ.get('USER', 'postgres')}", "dbname=postgres",
        "dbname=postgres user=postgres host=localhost",
    ]
    for dsn in candidates:
        try:
            admin = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        except Exception:
            continue
        try:
            admin.execute(f"DROP DATABASE IF EXISTS {PG_DATABASE}")
            admin.execute(f"CREATE DATABASE {PG_DATABASE}")
            base = " ".join(part for part in dsn.split() if not part.startswith("dbname="))
            conn = psycopg.connect(f"{base} dbname={PG_DATABASE}", connect_timeout=5, autocommit=True)
            conn.execute("SET enable_seqscan = off")  # se mide el índice GIN, no el plan que elegiría con tablas chicas
            return admin, conn
        except Exception as exc:
            print(f"  (no se pudo preparar PostgreSQL: {exc})")
            admin.close()
            return None
    print("  (no hay conexión a PostgreSQL: se omiten sus mediciones)")
    return None


def pg_close(handles) -> None:
    if handles is None:
        return
    admin, conn = handles
    conn.close()
    admin.execute(f"DROP DATABASE IF EXISTS {PG_DATABASE}")
    admin.close()


def pg_build(conn, docs: list[tuple[int, str]]) -> dict[str, Any]:
    conn.execute("DROP TABLE IF EXISTS docs")
    conn.execute("CREATE TABLE docs (id int PRIMARY KEY, cuerpo text)")
    with conn.cursor() as cur:
        with cur.copy("COPY docs (id, cuerpo) FROM STDIN") as copy:
            for row in docs:
                copy.write_row(row)
    t0 = time.perf_counter()
    conn.execute("CREATE INDEX docs_gin ON docs USING GIN (to_tsvector('english', cuerpo))")
    build_s = time.perf_counter() - t0
    conn.execute("ANALYZE docs")
    size = conn.execute("SELECT pg_relation_size('docs_gin')").fetchone()[0]
    heap = conn.execute("SELECT pg_relation_size('docs') / current_setting('block_size')::int").fetchone()[0]
    return {"n": len(docs), "engine": ENGINES[2], "build_s": build_s, "size_bytes": size, "terms": None,
            "postings": None, "vocab_pages": None, "postings_pages": None, "docs_pages": None, "heap_pages": heap,
            "build_reads": None, "build_writes": None, "runs": None}


def run_pg(conn, queries: dict[tuple[str, int], list[list[str]]], n: int,
           only: set[tuple[str, int]] | None = None) -> list[dict[str, Any]]:
    out = []
    for (kind, t), items in queries.items():
        if only is not None and (kind, t) not in only:
            continue
        ms, buffers, rows = [], [], []
        for words in items:
            sql = pg_sql(kind, words)
            plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql).fetchone()[0][0]
            node = plan["Plan"]
            ms.append(plan["Execution Time"])
            buffers.append(node.get("Shared Hit Blocks", 0) + node.get("Shared Read Blocks", 0))
            rows.append(node.get("Actual Rows", 0))
        mean, std = mean_std(ms)
        out.append({"n": n, "engine": ENGINES[2], "kind": kind, "terms": t, "queries": len(items), "ms_mean": mean,
                    "ms_std": std, "reads_mean": mean_std(buffers)[0], "index_reads_mean": None,
                    "table_reads_mean": None, "rows_mean": mean_std(rows)[0]})
    return out


# ---------------------------------------------------------------------- experimento
def run(quick: bool = False) -> None:
    sizes = QUICK_SIZES if quick else SIZES
    corpus = load_corpus(max(sizes))
    sizes = sorted({min(n, len(corpus)) for n in sizes})
    per_config = QUICK_QUERIES if quick else QUERIES
    handles = pg_connect()
    pg = handles[1] if handles else None
    build_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    scaling = {("Top-10", 3)}
    try:
        for n in sizes:
            docs = corpus[:n]
            name = f"exp6_{n}"
            work = fresh_dir(name)
            with open(work / "docs.csv", "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(docs)
            db = Database(work)
            db.execute("CREATE TABLE docs (id INT PRIMARY KEY, cuerpo TEXT(200)) USING HEAP")
            db.execute("COPY docs FROM 'docs.csv'")
            queries = make_queries(docs, per_config)
            last = n == sizes[-1]
            # sin índice: full scan
            query_rows += run_minidb(db, queries, n, ENGINES[1], None if last else scaling,
                                     limit=SCAN_QUERIES)
            t0 = time.perf_counter()
            m = db.execute(
                "CREATE INDEX idx_docs ON docs (cuerpo) USING INVERTED WITH (scoring = 'TFIDF_COSINE', analyzer = 'english')"
            )["metrics"]
            build_s = time.perf_counter() - t0
            index = db.tables["docs"].indexes["idx_docs"]
            s = index.structure.stats()
            build_rows.append({
                "n": n, "engine": ENGINES[0], "build_s": build_s, "size_bytes": index.size_bytes(),
                "terms": s["terms"], "postings": s["postings"], "vocab_pages": s["vocab_pages"],
                "postings_pages": s["postings_pages"], "docs_pages": s["docs_pages"],
                "heap_pages": db.tables["docs"].file.page_count, "build_reads": m["disk_reads"],
                "build_writes": m["disk_writes"], "runs": index.build_info.get("runs"),
            })
            query_rows += run_minidb(db, queries, n, ENGINES[0], None if last else scaling)
            if pg is not None:
                build_rows.append(pg_build(pg, docs))
                query_rows += run_pg(pg, queries, n, None if last else scaling)
            b = build_rows[-1 if pg is None else -2]
            print(f"  N={n:>6}: índice en {b['build_s']:.2f} s, {b['size_bytes'] / 1024:.0f} KB, "
                  f"{b['terms']} términos, {b['postings']} posteos", flush=True)
            db.close()
            cleanup(name)
    finally:
        pg_close(handles)
    write_csv("exp6_build.csv", BUILD_FIELDS, [[r[f] for f in BUILD_FIELDS] for r in build_rows])
    write_csv("exp6_queries.csv", QUERY_FIELDS, [[r[f] for f in QUERY_FIELDS] for r in query_rows])
    _plot(build_rows, query_rows)


def _plot(build_rows: list[dict[str, Any]], query_rows: list[dict[str, Any]]) -> None:
    fig, axes = new_figure(3, width=5.0)
    for engine in (ENGINES[0], ENGINES[2]):
        pts = sorted((r for r in build_rows if r["engine"] == engine), key=lambda r: r["n"])
        if not pts:
            continue
        axes[0].plot([r["n"] for r in pts], [r["build_s"] for r in pts], "o-", color=COLORS[engine], label=engine)
        axes[1].plot([r["n"] for r in pts], [r["size_bytes"] / 1024 for r in pts], "o-", color=COLORS[engine],
                     label=engine)
    style_axis(axes[0], "Tiempo de construcción del índice", "documentos", "segundos", logx=True, logy=True)
    style_axis(axes[1], "Tamaño del índice en disco", "documentos", "KB", logx=True, logy=True)
    axes[0].legend(fontsize=8)
    for engine in ENGINES:
        pts = sorted((r for r in query_rows if r["engine"] == engine and r["kind"] == "Top-10" and r["terms"] == 3),
                     key=lambda r: r["n"])
        if pts:
            axes[2].plot([r["n"] for r in pts], [r["ms_mean"] for r in pts], "o-", color=COLORS[engine], label=engine)
    style_axis(axes[2], "Top-10 con 3 términos: latencia vs. corpus", "documentos", "ms", logx=True, logy=True)
    axes[2].legend(fontsize=8)
    save_figure(fig, "exp6_build.png")

    largest = max(r["n"] for r in query_rows)
    rows = [r for r in query_rows if r["n"] == largest]
    labels = [f"{kind}\n{t} térm." for kind in KINDS for t in TERMS]
    fig, axes = new_figure(2, width=8.0, height=4.4)
    width = 0.27
    for j, engine in enumerate(ENGINES):
        by = {(r["kind"], r["terms"]): r for r in rows if r["engine"] == engine}
        if not by:
            continue
        xs = [i + (j - 1) * width for i in range(len(labels))]
        axes[0].bar(xs, [by.get((k, t), {}).get("ms_mean") or 0 for k in KINDS for t in TERMS], width,
                    color=COLORS[engine], label=engine)
        axes[1].bar(xs, [by.get((k, t), {}).get("reads_mean") or 0 for k in KINDS for t in TERMS], width,
                    color=COLORS[engine], label=engine)
    for ax, title, ylabel in ((axes[0], f"Latencia por consulta (N = {largest:,})", "ms"),
                              (axes[1], f"Bloques leídos por consulta (N = {largest:,})", "bloques (4 KB MiniDB · 8 KB PostgreSQL)")):
        style_axis(ax, title, "", ylabel, logy=True)
        ax.set_xticks(range(len(labels)), labels, fontsize=7)
    axes[0].legend(fontsize=8)
    save_figure(fig, "exp6_queries.png")


def replot() -> None:
    _plot(load_csv("exp6_build.csv"), load_csv("exp6_queries.csv"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experimento 6: índice invertido en disco vs. full scan e índice GIN.")
    parser.add_argument("--quick", action="store_true")
    run(parser.parse_args().quick)
