export interface SqlExample {
  label: string
  sql: string
}

export interface SqlExampleGroup {
  title: string
  items: SqlExample[]
}

export const SQL_EXAMPLES: SqlExampleGroup[] = [
  {
    title: 'Tabla HEAP',
    items: [
      {
        label: 'Crear tabla HEAP',
        sql: 'CREATE TABLE empleados (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING HEAP;',
      },
      { label: 'Carga masiva desde CSV', sql: "COPY empleados FROM 'empleados.csv';" },
      { label: 'Insertar un registro', sql: "INSERT INTO empleados VALUES (100001, 'Ada Lovelace', 'Analytics', 5200.0);" },
    ],
  },
  {
    title: 'Índices',
    items: [
      { label: 'Índice B+ sobre id', sql: 'CREATE INDEX idx_emp_id ON empleados (id) USING BTREE;' },
      { label: 'Índice hash extensible sobre id', sql: 'CREATE INDEX idx_emp_hash ON empleados (id) USING HASH;' },
    ],
  },
  {
    title: 'Consultas',
    items: [
      { label: 'Búsqueda por igualdad', sql: 'SELECT * FROM empleados WHERE id = 101;' },
      { label: 'Búsqueda por rango', sql: 'SELECT * FROM empleados WHERE id >= 100 AND id <= 500;' },
      {
        label: 'BETWEEN con filtro residual',
        sql: 'SELECT nombre, salario FROM empleados WHERE id BETWEEN 1000 AND 1100 AND salario > 3000;',
      },
      { label: 'Eliminar por id', sql: 'DELETE FROM empleados WHERE id = 101;' },
    ],
  },
  {
    title: 'Tabla SEQUENTIAL',
    items: [
      {
        label: 'Crear tabla SEQUENTIAL',
        sql: 'CREATE TABLE empleados_seq (id INT PRIMARY KEY, nombre CHAR(30), dept CHAR(20), salario FLOAT) USING SEQUENTIAL;',
      },
      { label: 'Carga masiva desde CSV', sql: "COPY empleados_seq FROM 'empleados.csv';" },
    ],
  },
  {
    title: 'Espacial (R-Tree)',
    items: [
      { label: 'Índice R-Tree sobre la ubicación', sql: 'CREATE INDEX idx_prod_geo ON productos (ubicacion) USING RTREE;' },
      {
        label: 'Productos a menos de 1.5 km de un punto',
        sql: 'SELECT id, titulo, tienda, ubicacion FROM productos WHERE ubicacion WITHIN_RADIUS((-77.0282, -12.1211), 1.5);',
      },
      {
        label: 'Los 10 más cercanos (k-NN)',
        sql: 'SELECT id, titulo, tienda, ubicacion, ubicacion <-> (-77.0282, -12.1211) AS km FROM productos ORDER BY km LIMIT 10;',
      },
      {
        label: 'Productos dentro de un rectángulo',
        sql: 'SELECT id, titulo, tienda, ubicacion FROM productos WHERE ubicacion WITHIN_BOX((-77.05, -12.13), (-77.01, -12.10));',
      },
    ],
  },
  {
    title: 'Texto (índice invertido)',
    items: [
      {
        label: 'Índice invertido sobre la descripción',
        sql: "CREATE INDEX idx_prod_texto ON productos (descripcion) USING INVERTED WITH (scoring = 'TFIDF_COSINE', analyzer = 'english');",
      },
      {
        label: 'Consulta booleana con ranking',
        sql: "SELECT id, titulo, COSINE_SCORE() AS relevancia FROM productos WHERE descripcion MATCH 'blue AND shirt AND NOT women' ORDER BY relevancia DESC LIMIT 10;",
      },
      {
        label: 'Texto libre con filtro de precio',
        sql: "SELECT id, titulo, precio, COSINE_SCORE() AS relevancia FROM productos WHERE descripcion MATCH 'running shoes' AND precio <= 150 ORDER BY relevancia DESC LIMIT 10;",
      },
    ],
  },
  {
    title: 'Vectorial (IVF)',
    items: [
      {
        label: 'Índice IVF sobre el embedding',
        sql: "CREATE INDEX idx_prod_vec ON productos (embedding) USING IVF WITH (metric = 'COSINE');",
      },
      { label: 'Celdas que se exploran por consulta', sql: 'SET ivf.probes = 5;' },
      {
        label: 'Vectores guardados (la búsqueda está en Recomendador)',
        sql:
          '-- Una consulta por similitud lleva un vector literal de 128 dimensiones:\n' +
          "--   SELECT id, titulo, embedding <=> '[0.12, -0.34, …]' AS distancia FROM productos ORDER BY distancia LIMIT 10;\n" +
          '-- La pestaña Recomendador lo arma a partir de una imagen. Vista previa de los vectores:\n' +
          'SELECT id, titulo, embedding FROM productos LIMIT 5;',
      },
    ],
  },
  {
    title: 'Híbridas',
    items: [
      { label: 'Estrategia de filtrado', sql: 'SET hybrid.strategy = auto;   -- auto | pre | post' },
      {
        label: 'Texto + precio + cercanía',
        sql: "SELECT id, titulo, precio, imagen, COSINE_SCORE() AS r FROM productos WHERE descripcion MATCH 'red dress' AND precio BETWEEN 50 AND 200 AND ubicacion WITHIN_RADIUS((-77.0282, -12.1211), 5) ORDER BY r DESC LIMIT 12;",
      },
      {
        label: 'Plan de una consulta de texto ordenada por precio',
        sql: "EXPLAIN SELECT id FROM productos WHERE descripcion MATCH 'watch' ORDER BY precio LIMIT 5;",
      },
    ],
  },
]
