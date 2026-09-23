"""Generador del dataset sintético ``empleados`` (solo biblioteca estándar).

Esquema: ``empleados(id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT)``.
Los ids son una permutación aleatoria de 1..N (únicos y en orden aleatorio), así
las inserciones no llegan ordenadas por la PK. Con la misma semilla el archivo
generado es idéntico.

Uso::

    python data/generate.py --n 100000 --seed 42            # -> data/empleados.csv
    python data/generate.py --n 500000 --out data/emp500k.csv
"""

from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path
from typing import Iterator

FIRST_NAMES = [
    "Ana", "Luis", "María", "José", "Carmen", "Jorge", "Lucía", "Carlos", "Rosa", "Miguel",
    "Elena", "Pedro", "Sofía", "Javier", "Valeria", "Diego", "Camila", "Andrés", "Daniela", "Fernando",
    "Gabriela", "Ricardo", "Paula", "Sergio", "Andrea", "Raúl", "Isabel", "Hugo", "Natalia", "Víctor",
    "Mónica", "Óscar", "Patricia", "Renzo", "Josué", "Ada", "Alan", "Grace", "Edgar", "Bárbara",
]
LAST_NAMES = [
    "García", "Rodríguez", "Pérez", "Sánchez", "Ramírez", "Torres", "Flores", "Rivera", "Gómez", "Díaz",
    "Vargas", "Castillo", "Rojas", "Mendoza", "Chávez", "Quispe", "Huamán", "Salazar", "Cruz", "Morales",
    "Ortiz", "Silva", "Reyes", "Gutiérrez", "Ruiz", "Paredes", "Lovelace", "Turing", "Hopper", "Codd",
]
DEPARTMENTS = [
    "Ventas", "Marketing", "Finanzas", "RRHH", "TI", "Operaciones", "Logística", "Legal",
    "Analytics", "Soporte", "Compras", "Calidad",
]
HEADER = ["id", "nombre", "dept", "salario"]


def generate_rows(n: int, seed: int = 42, *, sorted_ids: bool = False) -> Iterator[tuple[int, str, str, float]]:
    """Filas deterministas ``(id, nombre, dept, salario)``."""
    rng = random.Random(seed)
    ids = list(range(1, n + 1))
    if not sorted_ids:
        rng.shuffle(ids)
    for i in ids:
        nombre = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        dept = rng.choice(DEPARTMENTS)
        salario = round(rng.uniform(1025.0, 15000.0), 2)
        yield i, nombre, dept, salario


def write_csv(path: Path, n: int, seed: int = 42, *, sorted_ids: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(generate_rows(n, seed, sorted_ids=sorted_ids))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el CSV sintético de empleados")
    parser.add_argument("--n", type=int, default=100_000, help="número de registros (100k a 500k)")
    parser.add_argument("--seed", type=int, default=42, help="semilla del generador")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "empleados.csv")
    parser.add_argument("--sorted", action="store_true", help="ids en orden creciente (por defecto aleatorio)")
    args = parser.parse_args(argv)
    if args.n < 1:
        parser.error("--n debe ser positivo")
    path = write_csv(args.out, args.n, args.seed, sorted_ids=args.sorted)
    print(f"{args.n} registros escritos en {path} (semilla {args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
