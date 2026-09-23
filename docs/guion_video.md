# Guion del video demostrativo (5–10 minutos)

El enunciado pide que el video muestre: **(1)** la inspección de los archivos binarios en disco,
**(2)** la ejecución de consultas en el cliente web observando el plan de ejecución y **(3)** los
resultados del benchmark automatizado. Este guion dura ~8 minutos; cada bloque indica quién habla
(repartir entre los 3 integrantes) y exactamente qué mostrar.

## Preparación (antes de grabar)

```bash
docker compose up --build -d
docker compose exec backend python data/generate.py --n 100000 --seed 42
```

Abrir <http://localhost:5173> y dejar a mano una terminal en la raíz del repo. Si ya existían
tablas de una prueba anterior, borrarlas (`DROP TABLE empleados; DROP TABLE empleados_seq;`) para
empezar limpio. Grabar a 1920×1080 con zoom del navegador en 110–125 % para que se lean los números.

---

## 0:00 – 0:40 · Presentación (integrante 1)

- Qué es MiniDB: gestor relacional **en disco** hecho desde cero; todo pasa por páginas binarias de
  tamaño fijo (`seek` + `read`/`write` + `struct`), sin motores de BD, ORM ni `pickle`.
- Mostrar la arquitectura del README (sección 7) o la figura 1 del informe: cliente web → API →
  parser/planner/executor → Heap/Sequential/B+/Hash → DiskManager → archivos.

## 0:40 – 3:00 · Consultas en el cliente web con plan y métricas (integrante 2)

Mostrar los **4 paneles** a la vez: explorador, editor, resultados, plan y métricas.

1. Crear y cargar la tabla (menú **Ejemplos**), ejecutando cada sentencia con Ctrl+Enter:

   ```sql
   CREATE TABLE empleados (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING HEAP;
   COPY empleados FROM 'empleados.csv';
   ```

   Señalar en el explorador: 100 000 registros, 1 539 páginas, 65 registros por página de 4096 B.

2. Consulta puntual **sin índice** → plan `SeqScan`, **1 539 lecturas** (= P páginas):

   ```sql
   SELECT * FROM empleados WHERE id = 101;
   ```

3. Crear el B+ y repetir → plan `IndexScan (BTREE)`, **3 lecturas** (h = 2 + 1 para traer el registro);
   comparar en el gráfico de historial de I/O la caída de 1 539 a 3:

   ```sql
   CREATE INDEX idx_emp_id ON empleados (id) USING BTREE;
   SELECT * FROM empleados WHERE id = 101;
   ```

4. Rango → `IndexRangeScan`; mostrar el costo estimado vs el real y la lista de candidatos:

   ```sql
   SELECT * FROM empleados WHERE id >= 100 AND id <= 500;
   ```

   Paginar el resultado (401 filas, páginas de 50) para mostrar que la paginación la hace el backend.

5. Hash → `IndexScan (HASH)`, 3 lecturas (directorio + bucket + registro):

   ```sql
   CREATE INDEX idx_emp_hash ON empleados (id) USING HASH;
   DROP INDEX idx_emp_id;
   SELECT nombre, salario FROM empleados WHERE id = 99999;
   ```

6. Error de sintaxis con posición (el editor marca dónde está el error):

   ```sql
   SELECT * FORM empleados;
   ```

## 3:00 – 4:30 · Sequential File y reorganización (integrante 3)

```sql
CREATE TABLE empleados_seq (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING SEQUENTIAL;
COPY empleados_seq FROM 'empleados.csv';
SELECT * FROM empleados_seq WHERE id = 5000;                  -- BinarySearch: ~11 lecturas (log2 M)
SELECT * FROM empleados_seq WHERE id BETWEEN 1000 AND 1100;   -- SeqFileRangeScan: ~13 lecturas, ordenado
INSERT INTO empleados_seq VALUES (100001, 'Ada Lovelace', 'Analytics', 5200.0);
```

- Explicar el área principal ordenada + overflow encadenado y que la carga ya disparó
  reorganizaciones automáticas (mensaje del `COPY`; overflow < 10 % de las páginas principales).
- Pulsar **Reorganizar** en el explorador: mostrar páginas antes/después, overflow = 0 e I/O.
- Volver a `empleados` y ejecutar `SELECT * FROM empleados WHERE id >= 1 AND id <= 25000;` con el
  selector **Planner: Reglas** (regla del enunciado → `IndexRangeScan`, ~25 000 lecturas) y luego con
  **Costo** (el optimizador elige `SeqScan`, 1 539 lecturas): un índice no agrupado pierde frente al
  full scan cuando el rango es grande. Los candidatos con su costo estimado aparecen en el panel del plan.

## 4:30 – 6:30 · Inspección de los archivos binarios (integrante 1)

En la terminal:

```bash
ls -l data/                                                        # catalog.bin, .heap, .seq, .ovf, .hsh
docker compose exec backend python -m backend.tools.dump_page data/empleados.heap 0
docker compose exec backend python -m backend.tools.dump_page data/empleados.heap 1
docker compose exec backend python -m backend.tools.dump_page data/empleados_id.hsh 1
docker compose exec backend python -m backend.tools.dump_page data/empleados_seq.seq --all | head -20
```

Qué señalar:

- **Página 0** (metadatos): magic `MDB1`, `page_size = 4096`, cabeza de la free-list, nº de registros.
- **Página 1 del heap**: el header de 24 bytes en el hexdump (`page_id`, `page_type = 1`,
  `record_count = 65` → `41 00`, `free_space_offset = ffff` porque está llena, punteros `ff ff ff ff`
  = −1), después el **bitmap** (9 bytes `ff`…) y los registros de 62 bytes (el `id` en little-endian
  y el nombre en UTF-8 visible en la columna ASCII).
- **Directorio del hash**: profundidad global y punteros a buckets.
- **Sequential `--all`**: páginas `SEQ_MAIN` enlazadas con `next`/`prev` y `aux` = cabeza del overflow.

Luego abrir el **inspector de páginas** del cliente web (botón en el explorador) y mostrar una hoja
del B+ (claves ordenadas y RIDs, `next_leaf`/`prev_leaf`) y un nodo interno (claves y punteros).

## 6:30 – 8:00 · Resultados del benchmark automatizado (integrante 3)

```bash
python -m benchmarks.run_all --quick     # en vivo, ~30 s (la corrida completa tarda ~7 min)
ls benchmarks/results/
```

Mostrar las figuras de `benchmarks/results/` (o del informe) y decir la conclusión de cada una:

1. **Exp. 1 (`exp1_io_per_insert.png`)**: Heap = 2 I/O por inserción; B+ ≈ h + 1 y hash = 3 sobre el
   heap; Sequential con reorganización crece como log2 M; sin reorganización inserta barato pero la
   búsqueda posterior se vuelve lineal (`exp1_sequential_reorg.png`: ~4 000 lecturas vs 13).
2. **Exp. 2 (`exp2_equality.png`)**: 1 000 búsquedas: full scan 1 539 lecturas, búsqueda binaria 11.03,
   B+ 3, hash 3 — exactamente los costos teóricos.
3. **Exp. 3 (`exp3_range.png`)**: el B+ no agrupado cuesta ~k lecturas y cruza al full scan en
   k ≈ P (1.5 %); el Sequential agrupado es el mejor en todo el rango.
4. **Exp. 4 (`exp4_block_size.png`)**: al subir B el fan-out crece (125 → 1021) y la altura baja
   (3 → 2), pero los bytes transferidos crecen casi linealmente.

## 8:00 – 8:30 · Cierre (integrante 2)

Resumen: todo acceso a disco medido con `DiskCounter`, costos medidos = teóricos, 133 tests y
`docker compose up --build` levanta todo. Mencionar el informe (`docs/informe/informe.pdf`).
