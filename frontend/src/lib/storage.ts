// localStorage puede no existir o lanzar (modo privado, cuota llena): nunca debe romper la app.

export const STORAGE_KEYS = {
  editor: 'minidb.editor.sql',
  history: 'minidb.history.v1',
  pageSize: 'minidb.results.pageSize',
  theme: 'minidb.theme',
  chartScale: 'minidb.chart.log',
} as const

export function readString(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeString(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // sin almacenamiento disponible: se ignora
  }
}

export function readJson<T>(key: string, isValid: (value: unknown) => value is T): T | null {
  const raw = readString(key)
  if (raw === null) return null
  try {
    const value: unknown = JSON.parse(raw)
    return isValid(value) ? value : null
  } catch {
    return null
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    writeString(key, JSON.stringify(value))
  } catch {
    // valor no serializable: se ignora
  }
}
