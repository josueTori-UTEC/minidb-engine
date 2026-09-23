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

export function columnTypeLabel(col: Pick<ColumnInfo, 'type' | 'size'>): string {
  return col.size != null ? `${col.type}(${col.size})` : col.type
}

export interface FormattedCell {
  text: string
  numeric: boolean
  isNull: boolean
}

/** Formatea una celda de resultados: FLOAT con 2 decimales, enteros tal cual, NULL explícito. */
export function formatCell(value: CellValue | undefined, columnType?: string): FormattedCell {
  if (value === null || value === undefined) return { text: 'NULL', numeric: false, isNull: true }
  if (typeof value === 'number') {
    const isFloat = columnType === 'FLOAT' || !Number.isInteger(value)
    return { text: isFloat ? value.toFixed(2) : String(value), numeric: true, isNull: false }
  }
  if (typeof value === 'boolean') return { text: value ? 'true' : 'false', numeric: false, isNull: false }
  return { text: value, numeric: false, isNull: false }
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
