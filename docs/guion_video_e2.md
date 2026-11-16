# Guion del video demostrativo — Entregable 2 (5–10 minutos)

El video muestra **(1)** los índices nuevos y sus archivos en disco, **(2)** las consultas
espaciales, de texto, vectoriales e híbridas en el cliente web con su plan y su I/O, **(3)** la
aplicación de IA y **(4)** los experimentos 5 a 8. Dura ~9 minutos y está partido en **tres bloques
contiguos, uno por integrante**; cada bloque arranca y cierra solo, así que se graban por separado
y se pegan en orden.

| Bloque | Integrante | Duración | Qué necesita tener listo |
|---|---|---|---|
| 1. Índices multimodales en disco y SQL extendido | 1 | 0:00 – 3:30 | Catálogo `productos` cargado |
| 2. Planificador híbrido y aplicación de IA | 2 | 3:30 – 6:30 | Catálogo cargado, backend con `requirements-ml.txt` |
| 3. Experimentos 5 a 8 y cierre | 3 | 6:30 – 9:00 | Los CSV y PNG de `benchmarks/results/` |

---

## Antes de grabar (cada uno en su máquina)

```bash
docker compose up --build -d
docker compose exec backend python data/prepare_fashion.py --load
```

El segundo comando descarga el dataset, extrae las características, crea la tabla `productos`
(44 072 registros, 22 036 páginas) y construye los seis índices. **Se hace una sola vez y con la
grabación detenida**: tarda varios minutos. Con una máquina lenta, `--limit 5000` deja un catálogo
reducido con el que la demo funciona igual (los números de I/O serán menores que los de este guion).

Abrir <http://localhost:5173>, zoom del navegador en 110–125 %, 1920×1080. Antes de empezar,
comprobar en la pestaña **Recomendador** que el estado dice «catálogo cargado».

---

## Bloque 1 · Índices multimodales en disco y SQL extendido (integrante 1) — 0:00 a 3:30

### 0:00 – 0:35 · Qué cambia en el Entregable 2

- El motor del Entregable 1 (páginas de 4 KB, `seek` + `read`/`write`, `DiskCounter`) ahora guarda
  tres tipos nuevos: `POINT`, `TEXT` y `VECTOR(n)`.
- Tres índices nuevos, **todos en disco y paginados**: R-Tree, índice invertido e IVF.
- Mostrar la figura 1 del informe (`docs/informe/informe_e2.pdf`): capas del sistema.

### 0:35 – 1:15 · Los archivos

```bash
ls -l data/ | grep productos
docker compose exec backend python -m backend.tools.dump_page data/productos_ubicacion.rtr 0
```

Señalar: `.rtr` (R-Tree), `.vocab.dic` + `.postings.inv` + `.docs.bin` (índice invertido),
`.ivf.dir` + `.ivf.lst` (IVF). En la página 0 del R-Tree: `height = 3`, `leaf_capacity = 107`,
`internal_capacity = 113`, `entry_count = 44072`. El mismo inspector está en el cliente
(**Inspeccionar páginas**): abrir una hoja del R-Tree y una página de posteos.

### 1:15 – 2:10 · Espacial (R-Tree) — menú Ejemplos → Espacial

```sql
SELECT id, titulo, tienda, ubicacion, ubicacion <-> (-77.0282, -12.1211) AS km
FROM productos ORDER BY km LIMIT 10;
```

→ `RTreeKNN`, **19 lecturas** (9 del índice + 10 de la tabla) contra las 22 036 de un full scan.
Explicar la cola de prioridad por MINDIST. Abrir la vista **Mapa** del visor de resultados: los
puntos y los MBR que visitó la búsqueda.

```sql
SELECT id, titulo, tienda, ubicacion FROM productos
WHERE ubicacion WITHIN_RADIUS((-77.0282, -12.1211), 1.5);
```

→ `RTreeRadiusScan`: 31 lecturas del índice y 1 535 de la tabla, una por producto encontrado. Decir
en voz alta la lección: el índice poda casi todo, pero como **no es agrupado** el costo lo domina
traer los registros.

### 2:10 – 3:00 · Texto (índice invertido) — Ejemplos → Texto

```sql
SELECT id, titulo, COSINE_SCORE() AS relevancia FROM productos
WHERE descripcion MATCH 'blue AND shirt AND NOT women'
ORDER BY relevancia DESC LIMIT 10;
```

→ `InvertedIndexScan`, **177 lecturas** (167 del índice, 10 de la tabla). Explicar: búsqueda binaria
en el diccionario, mezcla de listas ordenadas con dos punteros para `AND` / `AND NOT`, puntaje
TF-IDF con normas precalculadas y Top-K con un *heap*. Solo se traen de la tabla los 10 ganadores.

### 3:00 – 3:30 · Vectorial (IVF)

```sql
SET ivf.probes = 5;
SELECT id, titulo, embedding FROM productos LIMIT 5;
```

Mostrar que el vector es una columna más (128 `float32` en el registro). La búsqueda por
similitud lleva un literal de 128 números, así que se muestra en el siguiente bloque desde el
Recomendador.

> **Frase de enlace:** «Cada índice resuelve su tipo de consulta. Lo interesante es combinarlos.»

---

## Bloque 2 · Planificador híbrido y aplicación de IA (integrante 2) — 3:30 a 6:30

### 3:30 – 4:45 · Pre-filtering vs post-filtering — Ejemplos → Híbridas

```sql
SELECT id, titulo, precio, imagen, COSINE_SCORE() AS r FROM productos
WHERE descripcion MATCH 'red dress' AND precio BETWEEN 50 AND 200
  AND ubicacion WITHIN_RADIUS((-77.0282, -12.1211), 5)
ORDER BY r DESC LIMIT 12;
```

En el panel de plan: la **estrategia** elegida, la explicación y el desglose de I/O
**índice / tabla** por archivo. Luego forzar cada estrategia y repetir la consulta:

```sql
SET hybrid.strategy = pre;    -- filtra primero con los índices, ordena los sobrevivientes
SET hybrid.strategy = post;   -- recorre el ranking y descarta los que no cumplen
SET hybrid.strategy = auto;
```

Con este filtro `pre` cuesta ~430 lecturas y `post` ~230 con el mismo resultado. Explicar cuándo
gana cada una: con un filtro **muy selectivo** el post-filtering recorre mucho ranking para
encontrar pocos sobrevivientes (o devuelve menos de K); con un filtro **poco selectivo** el
pre-filtering paga por materializar miles de RID. El planificador estima ambos y elige.

### 4:45 – 6:15 · El recomendador (pestaña Recomendador)

1. Elegir un producto de ejemplo (o subir una foto), fijar presupuesto y un punto en el mapa.
2. Método **Embedding + IVF**: mostrar los productos sugeridos, el mapa de tiendas y, abajo, **la
   sentencia SQL que generó la aplicación** (`embedding <=> [...]` con los filtros) y su plan:
   ~200 lecturas, pocos milisegundos.
3. Cambiar a **k-NN exacto**: mismo resultado visual, 22 036 lecturas y casi un segundo.
4. Cambiar a **BoVW (SIFT)**: resultados visiblemente peores (se parecen en textura, no en tipo
   de prenda). Es lo que mide el Experimento 7.
5. Añadir texto («red shoes») para mostrar la consulta que combina imagen, texto, precio y
   distancia.

Subrayar: **la aplicación solo habla con el motor por SQL**; no hay ninguna búsqueda fuera de él.

### 6:15 – 6:30 · Probes

Subir `probes` de 1 a 20 en el control del recomendador y ver cómo crecen las lecturas mientras
los resultados casi no cambian.

> **Frase de enlace:** «Eso fue una consulta. Ahora los números medidos sobre todo el catálogo.»

---

## Bloque 3 · Experimentos 5 a 8 y cierre (integrante 3) — 6:30 a 9:00

### 6:30 – 6:50 · Cómo se corren

```bash
python -m benchmarks.run_all --only 5
ls benchmarks/results/
```

Semilla fija, archivos nuevos por experimento; los resultados versionados vienen de la corrida
completa (`environment.csv` dice en qué máquina).

### 6:50 – 8:30 · Los cuatro experimentos

5. **R-Tree vs scan secuencial** (`exp5_knn.png`, `exp5_radius.png`, `exp5_dimension.png`): k-NN
   con k = 5 sobre 100 000 puntos, 12 lecturas contra 1 235. En radio, a partir de ~2 km el
   R-Tree lee **más** que el scan porque trae un registro por lectura. Al subir la dimensión el
   R-Tree deja de servir entre D = 8 y D = 16: la maldición de la dimensionalidad, y la razón de
   usar IVF para los embeddings.
6. **Índice invertido vs GIN de PostgreSQL** (`exp6_queries.png`): Top-10 en 128–252 lecturas y
   6–21 ms contra ~600 ms del recorrido completo; las consultas booleanas devuelven **exactamente
   las mismas filas** que PostgreSQL, que es 15–35× más rápido en ellas (C, buffer pool, posteos
   comprimidos). Decirlo sin rodeos.
7. **BoVW vs embeddings** (`exp7_precision.png`): Precision@10 de 0.51 contra 0.79, y el embedding
   además es más barato (216 lecturas y 6 ms contra 646 y 134 ms).
8. **ANN vs exacto** (`exp8_recall.png`): con 5 de 209 listas, Recall@10 = 95.9 % leyendo 212
   páginas en vez de 22 036; con 10 listas, 98.6 %. La curva recall–lecturas es el *trade-off*.

### 8:30 – 9:00 · Cierre

- **189 pruebas** (56 nuevas para los índices y las consultas multimodales), en GitHub Actions.
- `docker compose up --build` levanta todo; `prepare_fashion.py --load` deja el catálogo.
- Informe: `docs/informe/informe_e2.pdf`.

---

## Montaje

Pegar 1 → 2 → 3. Mismo zoom y mismo tema (claro u oscuro) en los tres bloques. Los números de
lecturas de este guion son deterministas con el catálogo completo; los tiempos cambian según la
máquina.
