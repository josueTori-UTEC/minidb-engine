import type { CellValue, ColumnInfo } from '../api'

const intFormat = new Intl.NumberFormat('es-PE', { maximumFractionDigits: 0 })

export function fmtInt(n: number | null | undefined): string {
  return n == null || !Number.isFinite(n) ? '—' : intFormat.format(n)
}

/** Milisegundos con precisión según la magnitud: 0.39 · 12.4 · 1,234 */
export function fmtMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  if (ms < 10) return ms.toFixed(2)
  if (ms < 100) return ms.toFixed(1)
  return intFormat.format(Math.round(ms))
}

/** Forma compacta para ejes: 1 · 10 · 100 · 1k · 10k · 1M */
export function fmtCompact(n: number): string {
  const abs = Math.abs(n)
  if (abs >= 1e6) return `${trimZero(n / 1e6)}M`
  if (abs >= 1e3) return `${trimZero(n / 1e3)}k`
  return trimZero(n)
}

function trimZero(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, '')
}

export function fmtBytes(n: number | null | undefined): string {
  return n == null ? '—' : `${intFormat.format(n)} B`
}

export function plural(n: number, singular: string, pluralForm: string): string {
  return `${fmtInt(n)} ${n === 1 ? singular : pluralForm}`
}

/** INT · FLOAT · POINT · CHAR(200) · VECTOR(128): el tamaño solo aporta en CHAR/TEXT (bytes) y VECTOR (dimensión). */
export function columnTypeLabel(col: Pick<ColumnInfo, 'type' | 'size'>): string {
  if (col.size == null || col.type === 'POINT' || col.type === 'INT' || col.type === 'FLOAT') return col.type
  return `${col.type}(${col.size})`
}

/** Número con a lo sumo `digits` decimales, sin ceros de relleno: -77.0282 · 0.5 · 12 */
export function fmtNumber(n: number, digits = 6): string {
  return Number.isFinite(n) ? String(Number(n.toFixed(digits))) : String(n)
}

/** Valor de una columna POINT: [lon, lat]. */
export function isPoint(value: unknown): value is [number, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    typeof value[0] === 'number' &&
    typeof value[1] === 'number' &&
    Number.isFinite(value[0]) &&
    Number.isFinite(value[1])
  )
}

export function formatPoint(point: readonly number[]): string {
  return `[${point.map((n) => fmtNumber(n)).join(', ')}]`
}

const VECTOR_PREVIEW = 3

/** Vector abreviado: [0.12, -0.34, 0.56, … (128)] */
export function formatVector(vector: readonly number[]): string {
  if (vector.length <= VECTOR_PREVIEW + 1) return `[${vector.map((n) => fmtNumber(n, 4)).join(', ')}]`
  const head = vector.slice(0, VECTOR_PREVIEW).map((n) => n.toFixed(2))
  return `[${head.join(', ')}, … (${vector.length})]`
}

export interface FormattedCell {
  text: string
  numeric: boolean
  isNull: boolean
  /** Valor completo cuando `text` está abreviado (vectores). */
  title?: string
}

/**
 * Formatea una celda de resultados: FLOAT con 2 decimales, enteros tal cual, NULL explícito.
 * Las columnas calculadas (distancias, puntajes) no tienen tipo en el esquema y usan 4 decimales.
 */
export function formatCell(value: CellValue | undefined, columnType?: string): FormattedCell {
  if (value === null || value === undefined) return { text: 'NULL', numeric: false, isNull: true }
  if (typeof value === 'number') {
    const text =
      columnType === 'FLOAT' ? value.toFixed(2)
      : Number.isInteger(value) ? String(value)
      : value.toFixed(columnType === undefined ? 4 : 2)
    return { text, numeric: true, isNull: false }
  }
  if (typeof value === 'boolean') return { text: value ? 'true' : 'false', numeric: false, isNull: false }
  if (Array.isArray(value)) {
    if (isPoint(value) && columnType !== 'VECTOR') return { text: formatPoint(value), numeric: false, isNull: false }
    return { text: formatVector(value), numeric: false, isNull: false, title: `[${value.join(', ')}]` }
  }
  return { text: value, numeric: false, isNull: false }
}

function csvField(value: CellValue | undefined): string {
  if (value === null || value === undefined) return ''
  const text = Array.isArray(value) ? `[${value.join(', ')}]` : String(value)
  return /["\n\r,]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

/**
 * Filas en CSV con cabecera, listo para pegar en una hoja de cálculo. Usa los valores
 * crudos (no los formateados para pantalla) y escapa según RFC 4180.
 */
export function rowsToCsv(columns: readonly string[], rows: readonly CellValue[][]): string {
  const lines = [columns.map(csvField).join(',')]
  for (const row of rows) lines.push(row.map(csvField).join(','))
  return lines.join('\n')
}

/**
 * El backend (Python) cuenta offsets en code points; JavaScript y CodeMirror en unidades UTF-16.
 * Solo difieren si el texto tiene caracteres fuera del BMP (por ejemplo, emojis).
 */
export function codePointOffsetToUtf16(text: string, cpOffset: number): number {
  let i = 0
  let cp = 0
  while (i < text.length && cp < cpOffset) {
    const code = text.charCodeAt(i)
    const isPair = code >= 0xd800 && code <= 0xdbff && i + 1 < text.length
    i += isPair ? 2 : 1
    cp += 1
  }
  return i
}

/** Línea y columna (1-based) de un offset UTF-16 dentro de un texto. */
export function lineColumnAt(text: string, offset: number): { line: number; column: number } {
  const before = text.slice(0, offset)
  const lines = before.split('\n')
  return { line: lines.length, column: (lines[lines.length - 1] ?? '').length + 1 }
}
