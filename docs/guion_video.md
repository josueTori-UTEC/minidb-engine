# Guion del video demostrativo (5–10 minutos)

El enunciado pide que el video muestre: **(1)** la inspección de los archivos binarios en disco,
**(2)** la ejecución de consultas en el cliente web observando el plan de ejecución y **(3)** los
resultados del benchmark automatizado. Este guion dura ~8 minutos y está partido en **tres bloques
contiguos, uno por integrante**, para que cada uno grabe el suyo por separado y después se monten en
orden. Cada bloque trae su propia preparación y arranca y cierra solo.

| Bloque | Integrante | Duración | Qué necesita tener listo |
|---|---|---|---|
| 1. El motor y sus archivos en disco | 1 | 0:00 – 2:30 | Solo la tabla `empleados` ya cargada |
| 2. El cliente SQL: planes y métricas | 2 | 2:30 – 5:45 | Ambas tablas cargadas **y los índices ya creados** |
| 3. Experimentos y cierre | 3 | 5:45 – 8:00 | Los CSV y PNG de `benchmarks/results/` |

> **El bloque 1 es el único que no ejecuta ninguna operación pesada.** Si alguien tiene que grabar
> solo y con una máquina lenta, ese es su bloque.

---

## Antes de grabar (cada uno en su máquina)

```bash
docker compose up --build -d
docker compose exec backend python data/generate.py --n 100000 --seed 42
```

Abrir <http://localhost:5173>. Grabar a 1920×1080 con el zoom del navegador en 110–125 % para que se
lean los números.

### Aviso importante sobre los tiempos

**No ejecutes `COPY` ni `CREATE INDEX` con la cámara grabando.** El motor no tiene buffer pool a
propósito (así el `DiskCounter` cuenta transferencias reales), de modo que cargar 100 000 registros
hace ~200 000 operaciones de página. Nativo son segundos, pero a través del volumen de Docker en
Windows —y peor si el repo está dentro de OneDrive— se va a **varios minutos**.

Deja todo cargado antes de grabar con este script, y en cámara muestra el **resultado**:

```sql
CREATE TABLE empleados (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING HEAP;
COPY empleados FROM 'empleados.csv';
CREATE INDEX idx_emp_id ON empleados (id) USING BTREE;
CREATE INDEX idx_emp_hash ON empleados (id) USING HASH;

CREATE TABLE empleados_seq (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING SEQUENTIAL;
COPY empleados_seq FROM 'empleados.csv';
```

Los archivos viven en `./data` y sobreviven a reiniciar los contenedores, así que esto se hace **una
sola vez**. Lo que cuesta cada paso, medido sobre 100 000 registros:

| Sentencia | Operaciones de página | Nativo | En Docker sobre Windows |
|---|---|---|---|
| `COPY empleados` (HEAP) | ~198 000 | ~4 s | ~3 min |
| `CREATE INDEX ... USING BTREE` | ~300 000 | ~16 s | ~4 min |
| `CREATE INDEX ... USING HASH` | ~300 000 | ~4 s | ~4 min |
| `COPY empleados_seq` (SEQUENTIAL) | **~1 070 000** | ~19 s | **~15 min** |

El `COPY` del Sequential es con diferencia el más caro: además de insertar, mantiene el orden y
dispara reorganizaciones. Lánzalo y vete a hacer otra cosa.

Si alguien quiere mostrar una carga en vivo de todas formas, que use `--n 20000`: sigue siendo un
volumen serio y tarda una fracción.

---

## Bloque 1 · El motor y sus archivos en disco (integrante 1) — 0:00 a 2:30

### 0:00 – 0:40 · Qué es MiniDB

- Gestor relacional **en disco** hecho desde cero: todo dato e índice vive en páginas binarias de
  tamaño fijo que se transfieren de a una con `seek` + `read`/`write` y `struct`.
- Sin motores de base de datos, sin ORM, sin `pickle` ni JSON para persistir, y nunca se lee un
  archivo completo en memoria.
- Mostrar el diagrama de capas del README (sección 7) o la figura 1 del informe:
  cliente web → API → parser/planner/executor → Heap/Sequential/B+/Hash → DiskManager → archivos.

### 0:40 – 1:10 · Lo que hay en disco

```bash
ls -l data/
```

Señalar los archivos y qué es cada uno: `catalog.bin` (el catálogo), `.heap`, `.seq` + `.ovf`
(área principal y overflow del Sequential), `.bpt` (árbol B+) y `.hsh` (hash extensible).

### 1:10 – 2:30 · Abrir las páginas por dentro

```bash
docker compose exec backend python -m backend.tools.dump_page data/empleados.heap 0
docker compose exec backend python -m backend.tools.dump_page data/empleados.heap 1
docker compose exec backend python -m backend.tools.dump_page data/empleados_id.bpt 1
```

Qué señalar en cada uno:

- **Página 0 (metadatos):** el magic `MDB1`, la versión, `page_size = 4096`, el número de páginas y
  la cabeza de la free-list.
- **Página 1 del heap:** los 24 bytes del header en el hexdump — `page_id`, `page_type = 1`,
  `record_count = 65` (`41 00` en little-endian), `free_space_offset = ff ff` porque la página está
  llena y los punteros en `ff ff ff ff` = −1. Después el **bitmap de presencia** (9 bytes, `ff`×8 +
  `01` = 65 slots ocupados) y los registros de 62 bytes: el `id` en little-endian y el nombre
  legible en la columna ASCII.
- **Nodo del B+:** las claves ordenadas con sus RID y los punteros `next_leaf` / `prev_leaf`.

Cerrar abriendo el mismo inspector desde el cliente web (botón **Inspeccionar páginas** en el
explorador), para mostrar que la API expone lo mismo por `GET /api/tables/{tabla}/pages/{id}`.

> **Frase de enlace:** «Eso es lo que hay en disco. Ahora veamos cómo se consulta.»

---

## Bloque 2 · El cliente SQL: planes y métricas (integrante 2) — 2:30 a 5:45

Arrancar mostrando los **4 paneles a la vez**: explorador, editor, resultados, y plan y métricas.
En el explorador se ve `empleados`: 100 000 registros, 1 539 páginas, 65 registros por página de
4 096 B.

### 2:30 – 3:00 · El costo de la carga

En el panel **Historial de I/O** ya está la entrada del `COPY`. Señalar:

- **98 461 lecturas y 100 001 escrituras.** No son 100 000 lecturas: faltan exactamente 1 539, que
  es el número de páginas. Cuando una fila estrena página, `allocate_page()` no lee nada y la
  inserción cuesta solo 1W en vez de 1R + 1W.
- El estimado del planner era 200 000 y el real 198 462: **−0.8 %**, justamente esas 1 539 lecturas
  que no ocurrieron.

### 3:00 – 4:15 · El planner elige el camino

Ejecutar con Ctrl+Enter y mirar el panel de plan en cada una. **Ninguna de estas consultas tarda
más de un segundo**, y el orden está pensado para no crear ningún índice en cámara.

**Sin índice aplicable.** `nombre` no tiene índice, así que no queda otra que el full scan:

```sql
SELECT * FROM empleados WHERE nombre = 'Ada Lovelace';
```

→ `SeqScan`, **1 539 lecturas** = P páginas, para 86 filas.

**Con índice sobre la PK:**

```sql
SELECT * FROM empleados WHERE id = 101;
```

→ `IndexScan`, **3 lecturas**. En la lista de candidatos del panel se ven los dos índices con su
costo estimado y el planner se queda con el más barato. Con el B+ son h = 2 lecturas para bajar
hasta la hoja más 1 para traer el registro por su RID.

En el gráfico del historial se ve la caída de 1 539 a 3. Ese es el mensaje central del proyecto.

### 4:15 – 5:00 · Rangos, hash y el optimizador

```sql
SELECT * FROM empleados WHERE id >= 100 AND id <= 500;
```

Plan `IndexRangeScan`, **404 lecturas** para 401 filas. Solo el B+ aparece como candidato: **el hash
no soporta rangos**. Mostrar el costo estimado junto al real. Paginar el resultado (401 filas en
páginas de 50) para dejar claro que **la paginación la hace el backend**, no el navegador. De paso,
el botón **Copiar CSV** del visor se lleva las filas al portapapeles.

Ahora un rango grande, cambiando el selector **Planner** de la barra del editor:

```sql
SELECT * FROM empleados WHERE id >= 1 AND id <= 25000;
```

- Con **Reglas** (la regla del enunciado): `IndexRangeScan`, ~25 000 lecturas.
- Con **Costo**: el optimizador elige `SeqScan`, 1 539 lecturas.

Explicar por qué: el índice **no es agrupado**, así que paga una lectura aleatoria por registro.
Frente al full scan solo conviene si k ≲ P, o sea con selectividad menor a P/N ≈ 1.5 %.

**Por último, el hash.** Ya no hacen falta más rangos, así que se puede borrar el B+ —`DROP INDEX` es
instantáneo, solo elimina el archivo— y repetir la consulta puntual:

```sql
DROP INDEX idx_emp_id;
SELECT * FROM empleados WHERE id = 101;
```

→ `IndexScan (HASH)`, **3 lecturas**: directorio + bucket + registro, independiente de N mientras no
haya overflow.

> **Este `DROP` va al final a propósito**, porque las consultas de rango necesitan el B+ y volver a
> crearlo sí es caro: ~300 000 operaciones de página, varios minutos en Docker. Si te hace falta el
> B+ para otra toma, recréalo con la grabación detenida.

### 5:00 – 5:30 · Sequential File y reorganización

```sql
SELECT * FROM empleados_seq WHERE id = 5000;                  -- BinarySearch: 11 lecturas ≈ log2 M
SELECT * FROM empleados_seq WHERE id BETWEEN 1000 AND 1100;   -- SeqFileRangeScan: 13 lecturas, ya ordenado
```

Explicar el área principal ordenada por rango de PK más la cadena de overflow por página, y que la
carga ya disparó reorganizaciones automáticas (el mensaje del `COPY` las reporta; el umbral es
overflow > 10 % de las páginas principales). Pulsar **Reorganizar** en el explorador y mostrar
páginas antes/después, overflow en 0 y el I/O que costó.

Contrastar: el B+ no agrupado pagó 404 lecturas por 401 filas; el Sequential, que **sí** está
agrupado por la PK, resuelve 101 filas en 13 lecturas de páginas contiguas.

### 5:30 – 5:45 · Errores

```sql
SELECT * FORM empleados;
```

El editor marca la posición exacta del error y la API devuelve 400 con línea y columna.

> **Frase de enlace:** «Todo esto son consultas sueltas. Veamos qué pasa cuando lo medimos en serio.»

---

## Bloque 3 · Experimentos y cierre (integrante 3) — 5:45 a 8:00

### 5:45 – 6:15 · Cómo se corren

```bash
python -m benchmarks.run_all --quick     # verificación rápida
ls benchmarks/results/
```

Si la máquina es lenta, correrlo antes de grabar y aquí solo mostrar la carpeta con los CSV y PNG ya
generados. Explicar que cada experimento usa archivos nuevos y semilla fija, y que los resultados
versionados salen de una corrida completa (`benchmarks/results/environment.csv` dice en qué máquina).

### 6:15 – 7:30 · Los cuatro experimentos

Mostrar las figuras y decir la conclusión de cada una:

1. **Exp. 1 — inserción masiva** (`exp1_io_per_insert.png`): el Heap cuesta 2 I/O por inserción sin
   importar N; el B+ ≈ h + 1 y el hash 3 sobre el heap; el Sequential con reorganización crece como
   log₂ M. En `exp1_sequential_reorg.png` se ve lo que compra reorganizar: sin ella la búsqueda
   posterior se vuelve lineal (~4 000 lecturas contra 13).
2. **Exp. 2 — búsquedas puntuales** (`exp2_equality.png`): 1 000 consultas sobre 100 000 registros.
   Full scan 1 539 lecturas, búsqueda binaria 11.03, B+ 3, hash 3 — **iguales a los costos teóricos**.
3. **Exp. 3 — rangos** (`exp3_range.png`): el B+ no agrupado cuesta ~k lecturas y cruza al full scan
   cerca del 1.5 % de selectividad; el Sequential, agrupado, gana en todo el rango.
4. **Exp. 4 — tamaño de bloque** (`exp4_block_size.png`): al subir B el fan-out crece de 125 a 1 021
   y la altura baja de 3 a 2, pero los bytes transferidos crecen casi linealmente. Menos I/O no es
   gratis: cada I/O mueve más datos.

### 7:30 – 8:00 · Cierre

- **Las lecturas son deterministas.** Repetimos los experimentos 2, 3 y 4 en una segunda máquina
  (macOS arm64 y Windows AMD64) y **todas las columnas de I/O salen idénticas, incluida la
  desviación estándar**; solo cambian los tiempos. Por eso el I/O es la métrica del informe y el
  tiempo es secundario.
- **133 pruebas** cubren desde el round-trip de una página hasta pruebas aleatorias diferenciales
  contra un modelo en memoria, y corren en GitHub Actions en cada push.
- **`docker compose up --build`** levanta todo en un solo paso.
- Mencionar el informe: `docs/informe/informe.pdf`.

---

## Montaje

Los tres bloques son contiguos y cada uno cierra con una frase de enlace, así que basta pegarlos en
orden 1 → 2 → 3. Conviene que los tres graben con el mismo zoom del navegador y el mismo tema (claro
u oscuro, el selector está en la cabecera) para que el corte no se note.
