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
from benchmarks.common import RESULTS_DIR, environment, write_csv

EXPERIMENTS = {
    1: ("Inserción masiva", exp1_insert.run),
    2: ("Búsquedas de igualdad", exp2_equality.run),
    3: ("Búsquedas por rango", exp3_range.run),
    4: ("Tamaño de bloque", exp4_block_size.run),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="tamaños reducidos (prueba rápida)")
    parser.add_argument("--only", type=int, nargs="+", choices=sorted(EXPERIMENTS), help="experimentos a correr")
    args = parser.parse_args()
    selected = args.only or sorted(EXPERIMENTS)
    started = time.perf_counter()
    timings = []
    for number in selected:
        title, fn = EXPERIMENTS[number]
        print(f"== Experimento {number}: {title}", flush=True)
        t0 = time.perf_counter()
        fn(quick=args.quick)
        timings.append((number, title, round(time.perf_counter() - t0, 1)))
    env = environment()
    write_csv(
        "environment.csv",
        ["key", "value"],
        [*env.items(), ("mode", "quick" if args.quick else "full"),
         *((f"exp{n}_seconds", s) for n, _, s in timings)],
    )
    print(f"Listo en {time.perf_counter() - started:.1f} s. Resultados en {RESULTS_DIR}")


if __name__ == "__main__":
    main()
