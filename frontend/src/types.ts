import type { ApiError, QueryResponse, ReorganizeResponse, StatementSummary } from './api'

/** Lo que muestra el visor de resultados. */
export type ResultState =
  | { status: 'idle' }
  | {
      status: 'success'
      response: QueryResponse
      /** Sentencias del script original (se conservan al paginar). */
      script: StatementSummary[]
      /** SQL que se reenvía al paginar; null si la última sentencia no fue un SELECT. */
      pageSql: string | null
    }
  | { status: 'error'; error: ApiError; sql: string; isSelection: boolean }

/** Lo que muestra el panel de plan y métricas. */
export type Activity =
  | { kind: 'query'; response: QueryResponse }
  | { kind: 'reorganize'; result: ReorganizeResponse }
  | { kind: 'error'; action: string; error: ApiError }

export interface HistoryEntry {
  /** Número correlativo (#n). */
  id: number
  /** Tipo de plan (SeqScan, IndexScan…) o de sentencia (CREATE TABLE, COPY…). */
  kind: string
  sql: string
  reads: number
  writes: number
  estimated: number | null
  totalMs: number
  /** Página pedida, si la ejecución vino de la paginación de resultados. */
  page?: number
}

export interface HistoryState {
  next: number
  items: HistoryEntry[]
}

export const HISTORY_LIMIT = 20
