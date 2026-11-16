"""Genera las tablas LaTeX del informe a partir de los CSV de ``benchmarks/results``
y copia los gráficos a ``docs/informe/figuras`` (así el informe se compila solo).

Uso: ``python -m benchmarks.report_tables``
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from benchmarks.common import RESULTS_DIR, ROOT, load_csv

INFORME = ROOT / "docs" / "informe"
TABLES = INFORME / "tablas"
FIGURES = INFORME / "figuras"


def _n(v: Any, digits: int = 0) -> str:
    """Número con separador de miles fino (estilo del informe)."""
    if v is None:
        return "--"
    if digits == 0:
        return f"{round(v):,}".replace(",", "\\,")
    return f"{v:,.{digits}f}".replace(",", "\\,")


def _write(name: str, body: str) -> None:
    TABLES.mkdir(parents=True, exist_ok=True)
    (TABLES / name).write_text(body, encoding="utf-8")


def exp1() -> None:
    rows = load_csv("exp1_insert.csv")
    sizes = sorted({r["n"] for r in rows})
    order = ["Heap", "Sequential sin reorg", "Sequential con reorg", "B+", "Hash"]
    lines = [
        "\\begin{tabular}{@{}l" + "r" * len(sizes) + "@{}}",
        "\\toprule",
        "Estructura & " + " & ".join(f"$N={_n(n)}$" for n in sizes) + " \\\\",
        "\\midrule",
        "\\multicolumn{" + str(len(sizes) + 1) + "}{@{}l}{\\emph{Tiempo total (s)}} \\\\",
    ]
    by = {(r["structure"], r["n"]): r for r in rows}
    for s in order:
        lines.append(f"{s} & " + " & ".join(_n(by[(s, n)]["time_s"], 2) for n in sizes) + " \\\\")
    lines.append("\\midrule")
    lines.append("\\multicolumn{" + str(len(sizes) + 1) + "}{@{}l}{\\emph{Escrituras de páginas}} \\\\")
    for s in order:
        lines.append(f"{s} & " + " & ".join(_n(by[(s, n)]["writes"]) for n in sizes) + " \\\\")
    lines.append("\\midrule")
    lines.append("\\multicolumn{" + str(len(sizes) + 1) + "}{@{}l}{\\emph{Lecturas de páginas}} \\\\")
    for s in order:
        lines.append(f"{s} & " + " & ".join(_n(by[(s, n)]["reads"]) for n in sizes) + " \\\\")
    lines.append("\\midrule")
    lines.append("\\multicolumn{" + str(len(sizes) + 1) + "}{@{}l}{\\emph{I/O por inserción (lecturas + escrituras) / N}} \\\\")
    for s in order:
        lines.append(f"{s} & " + " & ".join(_n(by[(s, n)]["io_per_insert"], 2) for n in sizes) + " \\\\")
    lines.append("\\midrule")
    lines.append("\\multicolumn{" + str(len(sizes) + 1) + "}{@{}l}{\\emph{Solo el índice (sin el heap): I/O por inserción}} \\\\")
    for s in ("B+", "Hash"):
        lines.append(f"{s} & " + " & ".join(_n(by[(s, n)]["index_io_per_insert"], 2) for n in sizes) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp1.tex", "\n".join(lines) + "\n")

    lines = [
        "\\begin{tabular}{@{}lrrrrrr@{}}",
        "\\toprule",
        "$N$ & \\multicolumn{3}{c}{Sequential sin reorg} & \\multicolumn{3}{c}{Sequential con reorg} \\\\",
        "\\cmidrule(lr){2-4}\\cmidrule(l){5-7}",
        " & $M$ & overflow & búsqueda & $M$ & overflow (reorgs) & búsqueda \\\\",
        "\\midrule",
    ]
    for n in sizes:
        a, b = by[("Sequential sin reorg", n)], by[("Sequential con reorg", n)]
        lines.append(
            f"{_n(n)} & {_n(a['main_pages'])} & {_n(a['overflow_pages'])} & {_n(a['search_reads_mean'], 1)} & "
            f"{_n(b['main_pages'])} & {_n(b['overflow_pages'])} ({_n(b['reorganizations'])}) & "
            f"{_n(b['search_reads_mean'], 1)} \\\\"
        )
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp1_seq.tex", "\n".join(lines) + "\n")

    lines = ["\\begin{tabular}{@{}lrrrrrr@{}}", "\\toprule",
             "Estructura & " + " & ".join(f"$N={_n(n)}$" for n in sizes) + " \\\\", "\\midrule"]
    for s in ("B+", "Hash"):
        key = "height" if s == "B+" else "global_depth"
        label = "B$^+$: altura $h$" if s == "B+" else "Hash: profundidad global"
        lines.append(f"{label} & " + " & ".join(_n(by[(s, n)][key]) for n in sizes) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp1_forma.tex", "\n".join(lines) + "\n")


def exp2() -> None:
    rows = load_csv("exp2_equality.csv")
    lines = [
        "\\begin{tabular}{@{}lrrrrrl@{}}",
        "\\toprule",
        "Método & lecturas (media) & $\\sigma$ & teórico & ms (media) & $\\sigma$ (ms) & parámetros \\\\",
        "\\midrule",
    ]
    for r in rows:
        lines.append(
            f"{r['method'].replace('B+', 'B$^+$')} & {_n(r['mean_reads'], 2)} & {_n(r['std_reads'], 2)} & "
            f"{_n(r['theoretical_reads'], 2) if isinstance(r['theoretical_reads'], float) else _n(r['theoretical_reads'])} & "
            f"{_n(r['mean_ms'], 3)} & {_n(r['std_ms'], 3)} & {r['parameters']} \\\\"
        )
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp2.tex", "\n".join(lines) + "\n")


def exp3() -> None:
    rows = load_csv("exp3_range.csv")
    sels = sorted({r["selectivity"] for r in rows})
    by = {(r["method"], r["selectivity"]): r for r in rows}
    methods = ["B+", "Sequential", "Full Scan (Heap)"]
    lines = [
        "\\begin{tabular}{@{}lr" + "rrr" * len(methods) + "@{}}",
        "\\toprule",
        " & & " + " & ".join(f"\\multicolumn{{3}}{{c}}{{{m.replace('B+', 'B$^+$')}}}" for m in methods) + " \\\\",
        "".join(f"\\cmidrule(lr){{{3 + 3 * i}-{5 + 3 * i}}}" for i in range(len(methods))),
        "Selectividad & $k$ & " + " & ".join("lecturas & teórico & ms" for _ in methods) + " \\\\",
        "\\midrule",
    ]
    for sel in sels:
        cells = []
        for m in methods:
            r = by[(m, sel)]
            cells.append(f"{_n(r['mean_reads'], 1)} & {_n(r['theoretical_reads'], 1)} & {_n(r['mean_ms'], 2)}")
        k = by[(methods[0], sel)]["k"]
        lines.append(f"{sel * 100:g}\\,\\% & {_n(k)} & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp3.tex", "\n".join(lines) + "\n")


def exp4() -> None:
    rows = load_csv("exp4_block_size.csv")
    lines = [
        "\\begin{tabular}{@{}l" + "r" * len(rows) + "@{}}",
        "\\toprule",
        "$B$ (bytes) & " + " & ".join(_n(r["page_size"]) for r in rows) + " \\\\",
        "\\midrule",
    ]
    spec = [
        ("registros por página del heap", "records_per_page", 0),
        ("páginas del heap ($P$)", "heap_pages", 0),
        ("entradas por hoja ($L$)", "leaf_capacity", 0),
        ("fan-out del interno ($m+1$)", "fan_out", 0),
        ("altura $h$ medida", "height", 0),
        ("altura teórica [mín, máx]", None, 0),
        ("hojas / internos", None, 0),
        ("I/O de inserción (heap + B$^+$)", "insert_io", 0),
        ("\\quad de los cuales en el B$^+$", "index_insert_io", 0),
        ("lecturas por búsqueda ($h+1$)", "search_reads_mean", 2),
        ("I/O total (inserción + 1000 búsquedas)", "total_io", 0),
        ("MB transferidos (I/O $\\times B$)", "mb_transferred", 1),
        ("tiempo de inserción (s)", "insert_time_s", 2),
        ("Sequential: $M$ / lecturas por búsqueda", None, 0),
        ("Hash: profundidad global / buckets", None, 0),
    ]
    for label, key, digits in spec:
        if key is not None:
            cells = [_n(r[key], digits) for r in rows]
        elif label.startswith("altura"):
            cells = [f"[{r['height_min_theory']}, {r['height_max_theory']}]" for r in rows]
        elif label.startswith("hojas"):
            cells = [f"{_n(r['leaf_pages'])} / {_n(r['internal_pages'])}" for r in rows]
        elif label.startswith("Sequential"):
            cells = [f"{_n(r['seq_main_pages'])} / {_n(r['seq_search_reads_mean'], 2)}" for r in rows]
        else:
            cells = [f"{_n(r['hash_global_depth'])} / {_n(r['hash_buckets'])}" for r in rows]
        lines.append(f"{label} & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp4.tex", "\n".join(lines) + "\n")


# ---------------------------------------------------------------------- Entrega 2
def exp5() -> None:
    rows = load_csv("exp5_spatial.csv")
    sizes = sorted({r["n"] for r in rows})
    methods = ["Secuencial (Heap)", "R-Tree (inserciones)", "R-Tree (STR)"]
    by = {(r["n"], r["method"], r["query"], r["param"]): r for r in rows}
    workload = [("radio", p) for p in sorted({r["param"] for r in rows if r["query"] == "radio"})] + \
               [("knn", p) for p in sorted({r["param"] for r in rows if r["query"] == "knn"})]
    lines = [
        "\\begin{tabular}{@{}ll" + "rr" * len(methods) + "r@{}}",
        "\\toprule",
        " & & " + " & ".join(f"\\multicolumn{{2}}{{c}}{{{m}}}" for m in methods) + " & \\\\",
        "".join(f"\\cmidrule(lr){{{3 + 2 * i}-{4 + 2 * i}}}" for i in range(len(methods))),
        "$N$ & consulta & " + " & ".join("lecturas & ms" for _ in methods) + " & filas \\\\",
        "\\midrule",
    ]
    for n in sizes:
        for i, (query, param) in enumerate(workload):
            label = f"radio {param:g} km" if query == "radio" else f"$k$-NN, $k={param:g}$"
            cells = []
            for m in methods:
                r = by[(n, m, query, param)]
                cells.append(f"{_n(r['reads_mean'], 1)} & {_n(r['ms_mean'], 2)}")
            first = _n(n) if i == 0 else ""
            lines.append(f"{first} & {label} & " + " & ".join(cells) +
                         f" & {_n(by[(n, methods[0], query, param)]['rows_mean'], 1)} \\\\")
        if n != sizes[-1]:
            lines.append("\\addlinespace")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp5.tex", "\n".join(lines) + "\n")

    lines = [
        "\\begin{tabular}{@{}rrrrrrrr@{}}",
        "\\toprule",
        "$N$ & páginas del Heap & \\multicolumn{3}{c}{R-Tree por inserciones} & \\multicolumn{3}{c}{R-Tree STR} \\\\",
        "\\cmidrule(lr){3-5}\\cmidrule(l){6-8}",
        " & & páginas & altura & construcción (s) / escrituras & páginas & altura & construcción (s) / escrituras \\\\",
        "\\midrule",
    ]
    for n in sizes:
        a = by[(n, methods[1], workload[0][0], workload[0][1])]
        b = by[(n, methods[2], workload[0][0], workload[0][1])]
        lines.append(
            f"{_n(n)} & {_n(a['heap_pages'])} & {_n(a['index_pages'])} & {a['height']} & "
            f"{_n(a['build_s'], 2)} / {_n(a['build_writes'])} & {_n(b['index_pages'])} & {b['height']} & "
            f"{_n(b['build_s'], 2)} / {_n(b['build_writes'])} \\\\"
        )
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp5_build.tex", "\n".join(lines) + "\n")

    rows = load_csv("exp5_dimension.csv")
    lines = [
        "\\begin{tabular}{@{}l" + "r" * len(rows) + "@{}}",
        "\\toprule",
        "Dimensión $D$ & " + " & ".join(str(r["dims"]) for r in rows) + " \\\\",
        "\\midrule",
    ]
    spec = [
        ("entradas por hoja", "leaf_capacity", 0),
        ("altura del R-Tree", "height", 0),
        ("páginas del R-Tree", "index_pages", 0),
        ("páginas del Heap ($P$)", "heap_pages", 0),
        ("R-Tree: lecturas por $k$-NN", "rtree_reads_mean", 1),
        ("R-Tree: \\% de hojas visitadas", "leaves_visited_pct", 1),
        ("Secuencial: lecturas", "seq_reads", 0),
        ("R-Tree: ms por consulta", "rtree_ms_mean", 2),
        ("Secuencial: ms por consulta", "seq_ms_mean", 2),
    ]
    for label, key, digits in spec:
        lines.append(f"{label} & " + " & ".join(_n(r[key], digits) for r in rows) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp5_dim.tex", "\n".join(lines) + "\n")


def exp6() -> None:
    build = load_csv("exp6_build.csv")
    sizes = sorted({r["n"] for r in build})
    by = {(r["n"], r["engine"]): r for r in build}
    mini, pg = "MiniDB (índice invertido)", "PostgreSQL (GIN)"
    has_pg = any(r["engine"] == pg for r in build)
    lines = [
        "\\begin{tabular}{@{}rrrrrrrr" + ("rr" if has_pg else "") + "@{}}",
        "\\toprule",
        " & \\multicolumn{7}{c}{MiniDB: índice invertido en disco}" + (" & \\multicolumn{2}{c}{PostgreSQL GIN}" if has_pg else "") + " \\\\",
        "\\cmidrule(lr){2-8}" + ("\\cmidrule(l){9-10}" if has_pg else ""),
        "documentos & términos & posteos & vocab. & posteos & docs & total (KB) & s" +
        (" & KB & s" if has_pg else "") + " \\\\",
        "\\midrule",
    ]
    for n in sizes:
        r = by[(n, mini)]
        cells = (f"{_n(n)} & {_n(r['terms'])} & {_n(r['postings'])} & {_n(r['vocab_pages'] - 1)} & "
                 f"{_n(r['postings_pages'] - 1)} & {_n(r['docs_pages'] - 1)} & {_n(r['size_bytes'] / 1024)} & "
                 f"{_n(r['build_s'], 2)}")
        if has_pg:
            g = by[(n, pg)]
            cells += f" & {_n(g['size_bytes'] / 1024)} & {_n(g['build_s'], 2)}"
        lines.append(cells + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp6_build.tex", "\n".join(lines) + "\n")

    queries = load_csv("exp6_queries.csv")
    largest = max(r["n"] for r in queries)
    engines = [e for e in ("MiniDB (índice invertido)", "MiniDB (full scan)", "PostgreSQL (GIN)")
               if any(r["engine"] == e and r["n"] == largest for r in queries)]
    by = {(r["engine"], r["kind"], r["terms"]): r for r in queries if r["n"] == largest}
    kinds = ["AND", "OR", "AND NOT", "Top-10"]
    lines = [
        "\\begin{tabular}{@{}lr" + "rr" * len(engines) + "r@{}}",
        "\\toprule",
        " & & " + " & ".join(f"\\multicolumn{{2}}{{c}}{{{e}}}" for e in engines) + " & \\\\",
        "".join(f"\\cmidrule(lr){{{3 + 2 * i}-{4 + 2 * i}}}" for i in range(len(engines))),
        "consulta & términos & " + " & ".join("ms & bloques" for _ in engines) + " & filas \\\\",
        "\\midrule",
    ]
    for kind in kinds:
        for i, t in enumerate(sorted({r["terms"] for r in queries})):
            cells = []
            for e in engines:
                r = by.get((e, kind, t))
                cells.append("-- & --" if r is None else f"{_n(r['ms_mean'], 2)} & {_n(r['reads_mean'], 1)}")
            ref = by[(engines[0], kind, t)]
            lines.append(f"{kind if i == 0 else ''} & {t} & " + " & ".join(cells) + f" & {_n(ref['rows_mean'], 1)} \\\\")
        if kind != kinds[-1]:
            lines.append("\\addlinespace")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp6_queries.tex", "\n".join(lines) + "\n")

    r_idx = {(r["kind"], r["terms"]): r for r in queries if r["n"] == largest and r["engine"] == engines[0]}
    lines = [
        "\\begin{tabular}{@{}lrrrr@{}}",
        "\\toprule",
        "consulta & términos & lecturas en el índice & lecturas en la tabla & filas \\\\",
        "\\midrule",
    ]
    for kind in kinds:
        for i, t in enumerate(sorted({r["terms"] for r in queries})):
            r = r_idx[(kind, t)]
            lines.append(f"{kind if i == 0 else ''} & {t} & {_n(r['index_reads_mean'], 1)} & "
                         f"{_n(r['table_reads_mean'], 1)} & {_n(r['rows_mean'], 1)} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp6_io.tex", "\n".join(lines) + "\n")


def exp7() -> None:
    precision = load_csv("exp7_precision.csv")
    cost = {r["method"]: r for r in load_csv("exp7_cost.csv")}
    methods = list(dict.fromkeys(r["method"] for r in precision))
    overall = {r["method"]: r for r in precision if r["scope"] == "global"}
    lines = [
        "\\begin{tabular}{@{}lrrrrrrr@{}}",
        "\\toprule",
        "Representación & P@5 & P@10 & extracción (ms/imagen) & consulta (ms) & lecturas & en el índice & índice (MB) \\\\",
        "\\midrule",
    ]
    for m in methods:
        o, c = overall[m], cost[m]
        lines.append(
            f"{m} & {_n(o['p_at_5'], 3)} & {_n(o['p_at_10'], 3)} & {_n(c['extraction_ms'], 2)} & "
            f"{_n(c['query_ms_mean'], 2)} & {_n(c['reads_mean'], 1)} & {_n(c['index_reads_mean'], 1)} & "
            f"{_n(c['index_mb'], 1) if c['index_mb'] else '--'} \\\\"
        )
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp7.tex", "\n".join(lines) + "\n")

    scopes = [r["scope"] for r in precision if r["method"] == methods[0] and r["scope"] != "global"]
    by = {(r["method"], r["scope"]): r for r in precision}
    lines = [
        "\\begin{tabular}{@{}l" + "rr" * len(methods) + "@{}}",
        "\\toprule",
        " & " + " & ".join(f"\\multicolumn{{2}}{{c}}{{{m.split(' (')[0]}{' exacto' if 'exacto' in m else (' + IVF' if 'IVF' in m else '')}}}" for m in methods) + " \\\\",
        "Tipo de artículo & " + " & ".join("P@5 & P@10" for _ in methods) + " \\\\",
        "\\midrule",
    ]
    for scope in scopes:
        cells = [f"{_n(by[(m, scope)]['p_at_5'], 2)} & {_n(by[(m, scope)]['p_at_10'], 2)}" for m in methods]
        lines.append(f"{scope} & " + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp7_tipos.tex", "\n".join(lines) + "\n")


def exp8() -> None:
    rows = load_csv("exp8_ann.csv")
    exact = next(r for r in rows if r["lists"] == 0)
    options = sorted({r["lists"] for r in rows if r["lists"]})
    lines = [
        "\\begin{tabular}{@{}rrrrrrrr@{}}",
        "\\toprule",
        "\\code{lists} & \\code{probes} & Recall@10 (\\%) & ms & lecturas & en el índice & en la tabla & vectores comparados (\\%) \\\\",
        "\\midrule",
    ]
    for lists in options:
        for i, r in enumerate(sorted((r for r in rows if r["lists"] == lists), key=lambda r: r["probes"])):
            lines.append(
                f"{_n(lists) if i == 0 else ''} & {_n(r['probes'])} & {_n(100 * r['recall_mean'], 1)} & "
                f"{_n(r['ms_mean'], 2)} & {_n(r['reads_mean'], 1)} & {_n(r['index_reads_mean'], 1)} & "
                f"{_n(r['table_reads_mean'], 1)} & {_n(r['scanned_pct'], 1)} \\\\"
            )
        lines.append("\\addlinespace")
    lines.append(
        f"\\multicolumn{{2}}{{@{{}}l}}{{$k$-NN exacto (secuencial)}} & 100.0 & {_n(exact['ms_mean'], 2)} & "
        f"{_n(exact['reads_mean'], 1)} & 0.0 & {_n(exact['table_reads_mean'], 1)} & 100.0 \\\\"
    )
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("exp8.tex", "\n".join(lines) + "\n")


def hybrid() -> None:
    rows = load_csv("hybrid_strategies.csv")
    names = {"pre": "\\emph{Pre-filtering}", "post": "\\emph{Post-filtering}"}
    lines = [
        "\\begin{tabular}{@{}llrrrrrl@{}}",
        "\\toprule",
        "Filtro (filas que lo cumplen) & Estrategia & filas & lecturas & en el índice & en la tabla & ms & elegida \\\\",
        "\\midrule",
    ]
    previous = None
    for r in rows:
        label = f"\\code{{{r['filter']}}} ({_n(r['selectivity_pct'], 1)}\\,\\%)" if r["filter"] != previous else ""
        if previous is not None and r["filter"] != previous:
            lines.append("\\addlinespace")
        previous = r["filter"]
        lines.append(
            f"{label} & {names[r['strategy']]} & {_n(r['rows_mean'], 1)} & {_n(r['reads_mean'])} & "
            f"{_n(r['index_reads_mean'])} & {_n(r['table_reads_mean'])} & {_n(r['ms_mean'], 1)} & "
            f"{'sí' if r['chosen_by_auto'] == 'sí' else ''} \\\\"
        )
    lines += ["\\bottomrule", "\\end{tabular}"]
    _write("hibrido.tex", "\n".join(lines) + "\n")


def summary() -> None:
    """Cifras del resumen del informe (una por archivo, para citarlas con \\input)."""
    spatial = load_csv("exp5_spatial.csv")
    largest = max(r["n"] for r in spatial)
    knn = next(r for r in spatial if r["n"] == largest and r["method"] == "R-Tree (STR)" and r["query"] == "knn"
               and r["param"] == 5)
    seq = next(r for r in spatial if r["n"] == largest and r["method"] == "Secuencial (Heap)" and r["query"] == "knn"
               and r["param"] == 5)
    ann = load_csv("exp8_ann.csv")
    exact = next(r for r in ann if r["lists"] == 0)
    base = sorted({r["lists"] for r in ann if r["lists"]})[1]
    pick = next(r for r in ann if r["lists"] == base and r["probes"] == 10)
    for name, value in (
        ("resumen_knn.tex", _n(knn["reads_mean"])),
        ("resumen_seq.tex", _n(seq["reads_mean"])),
        ("resumen_recall.tex", _n(100 * pick["recall_mean"], 1)),
        ("resumen_reads.tex", _n(100 * pick["reads_mean"] / exact["reads_mean"], 1)),
    ):
        _write(name, value + "%\n")


def environment() -> None:
    env = {r["key"]: r["value"] for r in load_csv("environment.csv")}
    body = (
        f"Python {env.get('python', '?')} en {env.get('platform', '?')} "
        f"({env.get('cpus', '?')} núcleos), corrida del {env.get('date', '?')}"
    )
    _write("entorno.tex", body.replace("_", "\\_") + "\n")


def environment_e2() -> None:
    env = {r["key"]: r["value"] for r in load_csv("environment.csv")}
    body = (
        f"Python {env.get('python', '?')} en {env.get('platform', '?')} "
        f"({env.get('cpus', '?')} núcleos), corrida del {env.get('date', '?')}"
    )
    _write("entorno_e2.tex", body.replace("_", "\\_") + "%\n")


def copy_figures() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    for png in RESULTS_DIR.glob("*.png"):
        shutil.copy2(png, FIGURES / png.name)


def main() -> None:
    exp1()
    exp2()
    exp3()
    exp4()
    for entrega2 in (exp5, exp6, exp7, exp8, hybrid, summary, environment_e2):
        try:
            entrega2()
        except FileNotFoundError as exc:  # el experimento todavía no se corrió
            print(f"  (se omite {entrega2.__name__}: falta {Path(exc.filename).name})")
    if not (TABLES / "entorno.tex").exists() or "exp5_seconds" not in {r["key"] for r in load_csv("environment.csv")}:
        environment()  # entorno de la corrida de los experimentos 1 a 4
    copy_figures()
    print(f"Tablas en {TABLES} y figuras en {FIGURES}")


if __name__ == "__main__":
    main()
