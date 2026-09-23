"""Ejecuta los 4 experimentos y resume los resultados en ``benchmarks/results/``.

Uso::

    python -m benchmarks.run_all            # corrida completa (N hasta 500 000)
    python -m benchmarks.run_all --quick    # corrida corta para verificar que todo funciona
    python -m benchmarks.run_all --only 2 3 # solo algunos experimentos
"""

from __future__ import annotations

import argparse
import time

from benchmarks import exp1_insert, exp2_equality, exp3_range, exp4_block_size
from benchmarks.common import RESULTS_DIR, environment, load_csv, write_csv

EXPERIMENTS = {
    1: ("Inserción masiva", exp1_insert),
    2: ("Búsquedas de igualdad", exp2_equality),
    3: ("Búsquedas por rango", exp3_range),
    4: ("Tamaño de bloque", exp4_block_size),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="tamaños reducidos (prueba rápida)")
    parser.add_argument("--only", type=int, nargs="+", choices=sorted(EXPERIMENTS), help="experimentos a correr")
    parser.add_argument("--replot", action="store_true", help="solo regenera los PNG desde los CSV existentes")
    args = parser.parse_args()
    selected = args.only or sorted(EXPERIMENTS)
    if args.replot:
        for number in selected:
            EXPERIMENTS[number][1].replot()
        print(f"Gráficos regenerados en {RESULTS_DIR}")
        return
    started = time.perf_counter()
    previous = {}
    if (RESULTS_DIR / "environment.csv").exists():
        previous = {row["key"]: row["value"] for row in load_csv("environment.csv")}
    for number in selected:
        title, module = EXPERIMENTS[number]
        print(f"== Experimento {number}: {title}", flush=True)
        t0 = time.perf_counter()
        module.run(quick=args.quick)
        previous[f"exp{number}_seconds"] = round(time.perf_counter() - t0, 1)
        previous[f"exp{number}_mode"] = "quick" if args.quick else "full"
    env = {**previous, **environment()}
    write_csv("environment.csv", ["key", "value"], sorted(env.items()))
    print(f"Listo en {time.perf_counter() - started:.1f} s. Resultados en {RESULTS_DIR}")


if __name__ == "__main__":
    main()
