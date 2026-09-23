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
