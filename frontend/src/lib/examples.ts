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
]
