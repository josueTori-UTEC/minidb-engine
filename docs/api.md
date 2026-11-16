# Contrato de la API REST de MiniDB

Base: `http://localhost:8000` (en Docker el frontend la usa a través del proxy de nginx en `/api`).
Todas las respuestas son JSON. Los tiempos están en milisegundos medidos con `time.perf_counter()`.

## `POST /api/query`

Ejecuta un script SQL (una o varias sentencias separadas por `;`). La respuesta principal
corresponde a la **última** sentencia; `statements` resume todas las ejecutadas.

Request:

```json
{ "sql": "SELECT * FROM empleados WHERE id = 101;", "page": 1, "page_size": 50 }
```

- `page` (>= 1, default 1) y `page_size` (1..1000, default 50): paginación de las filas del
  resultado. La consulta se ejecuta completa y se devuelve solo la página pedida.

Response `200`:

```json
{
  "statement": "SELECT",
  "columns": ["id", "nombre", "dept", "salario"],
  "rows": [[101, "Ada Lovelace", "Analytics", 5200.0]],
  "total_rows": 1,
  "page": 1,
  "page_size": 50,
  "message": "1 fila(s)",
  "plan": {
    "type": "IndexScan",
    "table": "empleados",
    "structure": "BTREE",
    "index": "idx_emp_id",
    "column": "id",
    "predicate": "id = 101",
    "filter": null,
    "estimated_io": 4,
    "detail": "B+ de altura 3: 3 lecturas hasta la hoja + 1 por registro",
    "candidates": [
      { "type": "IndexScan", "structure": "BTREE", "index": "idx_emp_id", "estimated_io": 4, "chosen": true },
      { "type": "SeqScan", "structure": "HEAP", "index": null, "estimated_io": 1539, "chosen": false }
    ]
  },
  "metrics": {
    "disk_reads": 4, "disk_writes": 0,
    "parse_ms": 0.08, "exec_ms": 0.31, "total_ms": 0.39
  },
  "statements": [
    {
      "sql": "SELECT * FROM empleados WHERE id = 101",
      "statement": "SELECT",
      "message": "1 fila(s)",
      "metrics": { "disk_reads": 4, "disk_writes": 0, "parse_ms": 0.08, "exec_ms": 0.31, "total_ms": 0.39 }
    }
  ]
}
```

- `statement`: `SELECT`, `INSERT`, `DELETE`, `CREATE TABLE`, `CREATE INDEX`, `DROP TABLE`,
  `DROP INDEX`, `COPY`, `EXPLAIN`.
- `columns`/`rows`: vacíos (`[]`) para sentencias que no devuelven filas.
- `plan`: `null` para DDL. Tipos posibles de `plan.type`: `SeqScan`, `IndexScan`,
  `IndexRangeScan`, `BinarySearch`, `SeqFileRangeScan` (SELECT/DELETE) e `Insert` (INSERT/COPY).
  `plan.structure`: `HEAP`, `SEQUENTIAL`, `BTREE` o `HASH`. `estimated_io` puede ser `null`.
- `metrics.disk_reads`/`disk_writes`: páginas leídas/escritas **exactas** medidas por el
  `DiskCounter` durante la sentencia (incluye mantenimiento de índices y metadatos).

Errores `400` (sintaxis o semántica) y `404` (tabla inexistente en endpoints de tablas):

```json
{
  "detail": {
    "error": "SyntaxError",
    "message": "Se esperaba FROM pero se encontró 'FORM'",
    "position": { "line": 1, "column": 10, "offset": 9 }
  }
}
```

`error` es `SyntaxError` o `SemanticError`; `position` es `null` en errores semánticos.

## `GET /api/tables`

```json
{
  "tables": [
    {
      "name": "empleados",
      "organization": "HEAP",
      "page_size": 4096,
      "primary_key": "id",
      "columns": [
        { "name": "id", "type": "INT", "size": null, "primary_key": true },
        { "name": "nombre", "type": "CHAR", "size": 30, "primary_key": false },
        { "name": "dept", "type": "CHAR", "size": 20, "primary_key": false },
        { "name": "salario", "type": "FLOAT", "size": null, "primary_key": false }
      ],
      "record_size": 62,
      "records_per_page": 65,
      "record_count": 100000,
      "page_count": 1539,
      "overflow_page_count": null,
      "indexes": [
        { "name": "idx_emp_id", "column": "id", "type": "BTREE", "height": 3, "global_depth": null,
          "page_count": 353, "entry_count": 100000 },
        { "name": "idx_emp_hash", "column": "id", "type": "HASH", "height": null, "global_depth": 9,
          "page_count": 402, "entry_count": 100000 }
      ]
    }
  ]
}
```

`page_count` son páginas de datos (sin la página 0 de metadatos). `overflow_page_count` solo
aplica a `SEQUENTIAL` (en `HEAP` es `null`).

## `POST /api/tables/reorganize`

Request `{ "table": "empleados" }` (solo tablas `SEQUENTIAL`). Response:

```json
{
  "table": "empleados",
  "message": "Reorganización completa: 2084 → 2086 páginas principales, overflow vaciado",
  "before": { "main_pages": 2084, "overflow_pages": 210, "record_count": 100000 },
  "after": { "main_pages": 2086, "overflow_pages": 0, "record_count": 100000 },
  "metrics": { "disk_reads": 2296, "disk_writes": 2890, "parse_ms": 0.0, "exec_ms": 85.2, "total_ms": 85.2 }
}
```

## `GET /api/tables/{name}/files`

Lista los archivos binarios inspeccionables de la tabla:

```json
{
  "table": "empleados",
  "files": [
    { "key": "data", "kind": "HEAP", "path": "empleados.heap", "num_pages": 1540, "page_size": 4096 },
    { "key": "idx_emp_id", "kind": "BTREE", "path": "empleados_id.bpt", "num_pages": 354, "page_size": 4096 }
  ]
}
```

`key` es `data` (archivo principal), `overflow` (solo SEQUENTIAL) o el nombre de un índice.

## `GET /api/tables/{name}/pages/{page_id}?file=<key>`

Decodifica una página (por defecto `file=data`). Pensado para el video y el inspector del
frontend.

```json
{
  "table": "empleados",
  "file": "data",
  "path": "empleados.heap",
  "page_id": 1,
  "page_size": 4096,
  "num_pages": 1540,
  "header": {
    "page_id": 1, "page_type": 1, "page_type_name": "HEAP_DATA", "flags": 0,
    "record_count": 65, "free_space_offset": 65535,
    "next_page_id": -1, "prev_page_id": -1, "aux_page_id": -1
  },
  "details": {
    "capacity": 65,
    "occupied_slots": [0, 1, 2],
    "records": [ { "slot": 0, "values": [4821, "Ana Torres", "Ventas", 3150.5] } ]
  },
  "hexdump": "00000000  01 00 00 00 01 00 41 00  ff ff ff ff ff ff ff ff  |......A.........|\n..."
}
```

`details` depende del tipo de página: metadatos (página 0), datos (bitmap y registros), nodos B+
(claves, hijos o RIDs) y buckets/directorio hash.

## `GET /api/health`

`{ "status": "ok", "data_dir": "/app/data", "tables": 1 }`

---

# Entrega 2: extensión multimodal

## Cambios en `POST /api/query`

El contrato no cambia; se agregan campos.

**Valores.** Una columna `POINT` llega como `[lon, lat]` y una `VECTOR(n)` como un arreglo de `n`
números. Las expresiones `COSINE_SCORE()`, `col <-> x` y `col <=> x` son columnas numéricas.

**`metrics.io`** — desglose de bloques por archivo, separando el índice de la tabla de datos:

```json
"metrics": {
  "disk_reads": 280, "disk_writes": 0, "parse_ms": 0.31, "exec_ms": 6.82, "total_ms": 7.13,
  "io": {
    "index_reads": 268, "index_writes": 0, "table_reads": 12, "table_writes": 0,
    "other_reads": 0, "other_writes": 0,
    "files": [
      { "file": "productos.heap", "role": "table", "owner": "productos", "reads": 12, "writes": 0 },
      { "file": "productos_embedding.ivf.dir", "role": "index", "owner": "idx_prod_vec", "reads": 30, "writes": 0 },
      { "file": "productos_embedding.ivf.lst", "role": "index", "owner": "idx_prod_vec", "reads": 238, "writes": 0 }
    ]
  }
}
```

**`plan`** — además de los campos de la Entrega 1:

| Campo | Contenido |
|---|---|
| `type` | también `RTreeRadiusScan`, `RTreeWindowScan`, `RTreeKNN`, `InvertedIndexScan`, `IVFScan` |
| `structure` | también `RTREE`, `INVERTED`, `IVF` |
| `strategy` | `"pre-filter"`, `"post-filter"` o `null` (consultas híbridas: filtro relacional + ranking) |
| `order_by`, `limit` | el `ORDER BY` y el `LIMIT` de la consulta |
| `warning` | aviso si el post-filtering devolvió menos filas que el `LIMIT` |
| `candidates[].strategy` | estrategia de cada camino evaluado por el planificador |
| `spatial` | `{kind: "radius"\|"box"\|"knn", center, radius_km, box, k, candidates, nodes_visited, leaves_visited, mbrs}`; `mbrs` son las cajas `[x_min, y_min, x_max, y_max]` de los nodos del R-Tree inspeccionados (hasta 400) |
| `text` | `{mode: "boolean"\|"ranked", scoring, matched, rebuilt, terms: [{term, df, idf, posting_pages}]}` |
| `vector` | `{metric, probes, lists, probed, dir_pages, list_pages, scanned, fetched, rebuilt}` |
| `prefilter_rids` | RID entregados por el índice relacional en el pre-filtering textual |

Sentencia nueva: `SET nombre = valor` (`ivf.probes`, `hybrid.strategy`, `text.analyzer`); responde como
una DDL (`statement: "SET"`, `message`).

## Cambios en `GET /api/tables`

Columnas con `type` `POINT` o `VECTOR` (`size` = dimensión). Cada índice agrega `files` (un índice
invertido tiene tres archivos y un IVF dos), `options` (las de `WITH (...)`) y `details`:

| `type` | `details` |
|---|---|
| `RTREE` | `leaf_capacity`, `internal_capacity` (la altura va en `height`) |
| `INVERTED` | `terms`, `documents`, `postings`, `avg_length`, `analyzer`, `scoring`, `stale`, `vocab_pages`, `postings_pages`, `docs_pages` |
| `IVF` | `dims`, `metric`, `lists`, `trained`, `vectors`, `list_capacity`, `dir_pages`, `list_pages` |

En `GET /api/tables/{name}/files` un índice de varios archivos aparece una vez por archivo, con
`key` = `índice:archivo` (por ejemplo `idx_prod_texto:postings.inv`); esa clave es el parámetro
`file` de `GET /api/tables/{name}/pages/{page_id}`.

## Aplicación de IA (recomendador multimodal)

### `GET /api/app/status`

```json
{ "table": "productos", "loaded": true, "models": true, "media": true,
  "info": { "records": 44072, "pages": 22036,
            "indexes": [ { "name": "idx_prod_vec", "type": "IVF", "column": "embedding" } ] },
  "methods": { "embedding": "Embedding ResNet-18 + IVF", "bovw": "BoVW (SIFT) + índice invertido",
               "exact": "k-NN exacto (recorrido secuencial)", "text": "Texto: TF-IDF + coseno" } }
```

`loaded` es `false` hasta que se carga el catálogo (`python data/prepare_fashion.py --load`).

### `GET /api/app/samples?n=12&seed=0`

Productos de ejemplo para usar como imagen de consulta:
`{ "products": [ { "id", "titulo", "categoria", "tipo", "precio", "imagen" } ] }`.

### `GET /api/media/{id}.jpg`

Imagen del producto (JPEG) guardada en `<MINIDB_DATA_DIR>/media`.

### `POST /api/app/recommend` (`multipart/form-data`)

| Campo | Tipo | Uso |
|---|---|---|
| `image` | archivo | foto de la prenda (≤ 8 MB) |
| `product_id` | int | usar la imagen de un producto del catálogo |
| `text` | string | descripción en lenguaje natural (el catálogo está en inglés) |
| `price_min`, `price_max` | float | rango de precios (índice B+) |
| `lon`, `lat`, `radius_km` | float | cercanía a un punto (R-Tree) |
| `category` | string | categoría exacta (`apparel`, `footwear`, ...) |
| `k` | int | resultados por método (default 12, máximo 60) |
| `probes` | int | `ivf.probes` (default 5) |
| `strategy` | `auto` \| `pre` \| `post` | estrategia de las consultas híbridas |
| `methods` | lista separada por comas | `embedding`, `bovw`, `exact`, `text`; por defecto `embedding,bovw` con imagen y `text` sin ella |

Debe venir una imagen, un `product_id` o un `text`. La aplicación vectoriza la imagen (ResNet-18 +
PCA y SIFT + codebook), arma una sentencia SQL por método y la ejecuta en el motor:

```json
{
  "query": { "text": "", "has_image": true, "k": 12, "probes": 5, "strategy": "auto", "product_id": 15970,
             "filters": { "price_min": 20, "price_max": 80, "lon": -77.0282, "lat": -12.1211,
                          "radius_km": 6, "category": "" } },
  "features": { "keypoints": 100, "visual_words": 46, "embedding_ms": 35.2, "bovw_ms": 6.1,
                "width": 60, "height": 80 },
  "results": [
    { "method": "embedding", "label": "Embedding ResNet-18 + IVF", "score_column": "distancia",
      "sql": "SELECT id, titulo, ..., embedding <=> '[-0.73, 0.15, -0.00, … (128 dims)]' AS distancia FROM productos WHERE precio BETWEEN 20.0 AND 80.0 AND ubicacion WITHIN_RADIUS((-77.0282, -12.1211), 6.0) ORDER BY distancia ASC LIMIT 12",
      "columns": ["id", "titulo", "categoria", "tipo", "color", "precio", "tienda", "ubicacion", "imagen", "distancia"],
      "rows": [[15970, "Turtle Check Men Navy Blue Shirt", "apparel", "Shirts", "Navy Blue", 46.97,
                "La Victoria 5", [-77.019484, -12.070601], "15970.jpg", 0.0]],
      "total_rows": 12, "plan": { "type": "IVFScan", "strategy": "post-filter", "...": "..." },
      "metrics": { "disk_reads": 293, "exec_ms": 6.8, "io": { "index_reads": 268, "table_reads": 25 } },
      "error": null }
  ]
}
```

`sql` es la sentencia abreviada para mostrar (el vector y las palabras visuales completos no se
incluyen). Si un método falla trae `error` y `rows: []`. Errores globales: `400` con el mismo
formato que `/api/query`.
