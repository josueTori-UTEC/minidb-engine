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


def environment() -> None:
    env = {r["key"]: r["value"] for r in load_csv("environment.csv")}
    body = (
        f"Python {env.get('python', '?')} en {env.get('platform', '?')} "
        f"({env.get('cpus', '?')} núcleos), corrida del {env.get('date', '?')}"
    )
    _write("entorno.tex", body.replace("_", "\\_") + "\n")


def copy_figures() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    for png in RESULTS_DIR.glob("*.png"):
        shutil.copy2(png, FIGURES / png.name)


def main() -> None:
    exp1()
    exp2()
    exp3()
    exp4()
    environment()
    copy_figures()
    print(f"Tablas en {TABLES} y figuras en {FIGURES}")


if __name__ == "__main__":
    main()
