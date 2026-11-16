import type { ReactNode } from 'react'

const PLAN_TONE: Record<string, string> = {
  SeqScan: 'seqscan',
  IndexScan: 'indexscan',
  IndexRangeScan: 'rangescan',
  BinarySearch: 'binsearch',
  SeqFileRangeScan: 'seqrange',
  Insert: 'insert',
  RTreeRadiusScan: 'rtree',
  RTreeWindowScan: 'rtree',
  RTreeKNN: 'rtree',
  InvertedIndexScan: 'inverted',
  IVFScan: 'ivf',
}

export const PLAN_DESCRIPTION: Record<string, string> = {
  SeqScan: 'Recorrido secuencial: lee todas las páginas de datos de la tabla.',
  IndexScan: 'Búsqueda puntual en el índice y acceso directo al registro por su RID.',
  IndexRangeScan: 'Descenso en el B+ hasta la primera clave y recorrido por las hojas enlazadas.',
  BinarySearch: 'Búsqueda binaria sobre las páginas ordenadas del archivo secuencial.',
  SeqFileRangeScan: 'Búsqueda binaria del inicio del rango y recorrido ordenado del archivo secuencial.',
  Insert: 'Inserción en el archivo de datos y mantenimiento de todos sus índices.',
  RTreeRadiusScan: 'Descenso por el R-Tree visitando solo los nodos cuyo MBR intersecta el círculo de búsqueda.',
  RTreeWindowScan: 'Descenso por el R-Tree visitando solo los nodos cuyo MBR intersecta el rectángulo de búsqueda.',
  RTreeKNN: 'Vecinos más cercanos sobre el R-Tree, expandiendo primero los nodos de menor distancia al punto.',
  InvertedIndexScan: 'Lectura de las listas de posteo de los términos de la consulta en el índice invertido.',
  IVFScan: 'Búsqueda aproximada: se comparan solo los vectores de las celdas más cercanas a la consulta.',
}

const STRUCTURE_TONE: Record<string, string> = {
  HEAP: 'heap',
  SEQUENTIAL: 'sequential',
  BTREE: 'btree',
  HASH: 'hash',
  RTREE: 'rtree',
  INVERTED: 'inverted',
  IVF: 'ivf',
  OVERFLOW: 'sequential',
}

export const STRUCTURE_DESCRIPTION: Record<string, string> = {
  HEAP: 'Heap file con free-list de páginas',
  SEQUENTIAL: 'Archivo secuencial ordenado por PK, con área de overflow',
  BTREE: 'Árbol B+ en disco (no agrupado)',
  HASH: 'Hashing extensible en disco',
  RTREE: 'R-Tree en disco para puntos (rectángulos mínimos, MBR)',
  INVERTED: 'Índice invertido en disco: vocabulario, listas de posteo y normas',
  IVF: 'Índice vectorial IVF: centroides y listas invertidas de vectores',
}

const STATEMENT_TONE: Record<string, string> = {
  SELECT: 'select',
  EXPLAIN: 'select',
  INSERT: 'insert',
  COPY: 'copy',
  DELETE: 'delete',
  'DROP TABLE': 'delete',
  'DROP INDEX': 'delete',
  'CREATE TABLE': 'ddl',
  'CREATE INDEX': 'ddl',
  SET: 'ddl',
  REORGANIZE: 'reorg',
}

type BadgeSize = 'xs' | 'sm' | 'lg'

interface BadgeProps {
  tone: string
  size?: BadgeSize
  title?: string
  children: ReactNode
}

export function Badge({ tone, size = 'sm', title, children }: BadgeProps) {
  return (
    <span className={`badge badge-${size} tone-${tone}`} title={title}>
      {children}
    </span>
  )
}

export function PlanBadge({ type, size }: { type: string; size?: BadgeSize }) {
  return (
    <Badge tone={PLAN_TONE[type] ?? 'neutral'} size={size} title={PLAN_DESCRIPTION[type]}>
      {type}
    </Badge>
  )
}

export function StructureBadge({ structure, size = 'xs' }: { structure: string; size?: BadgeSize }) {
  return (
    <Badge tone={STRUCTURE_TONE[structure] ?? 'neutral'} size={size} title={STRUCTURE_DESCRIPTION[structure]}>
      {structure}
    </Badge>
  )
}

export function StatementBadge({ statement, size = 'xs' }: { statement: string; size?: BadgeSize }) {
  return (
    <Badge tone={STATEMENT_TONE[statement] ?? 'neutral'} size={size}>
      {statement}
    </Badge>
  )
}
