import { useMemo, useState } from 'react'
import type { ApiError, CellValue, ColumnInfo, QueryResponse, StatementSummary, TableInfo } from '../api'
import { codePointOffsetToUtf16, columnTypeLabel, fmtInt, fmtMs, formatCell, lineColumnAt } from '../lib/format'
import { RUN_SHORTCUT } from '../lib/platform'
import type { ResultState } from '../types'
import { Badge, StatementBadge } from './Badges'
import { Icon } from './Icon'

export const PAGE_SIZES = [25, 50, 100, 200] as const

interface ResultsPanelProps {
  result: ResultState
  busy: boolean
  pageSize: number
  tables: readonly TableInfo[]
  onPage(page: number): void
  onPageSize(size: number): void
}

export function ResultsPanel({ result, busy, pageSize, tables, onPage, onPageSize }: ResultsPanelProps) {
  return (
    <section className="panel panel-results" aria-labelledby="results-title" aria-busy={busy}>
      <header className="panel-header">
        <h2 className="panel-title" id="results-title">
          <Icon name="table" />
          Resultados
        </h2>
        <div className="panel-actions results-summary" aria-live="polite">
          <ResultSummary result={result} busy={busy} />
        </div>
      </header>
      {busy && <div className="progress-bar" aria-hidden="true" />}

      <div className={`panel-body results-body${busy ? ' is-busy' : ''}`}>
        {result.status === 'idle' && (
          <div className="empty-state">
            <Icon name="table" size={28} className="empty-icon" />
            <p className="empty-title">Sin resultados todavía</p>
            <p className="empty-text">
              Escribe una consulta y presiona <kbd>{RUN_SHORTCUT}</kbd>. Si hay texto seleccionado, solo se ejecuta la
              selección.
            </p>
          </div>
        )}
        {result.status === 'error' && <ErrorView error={result.error} sql={result.sql} isSelection={result.isSelection} />}
        {result.status === 'success' && (
          <SuccessView
            response={result.response}
            script={result.script}
            canPaginate={result.pageSql !== null}
            busy={busy}
            pageSize={pageSize}
            tables={tables}
            onPage={onPage}
            onPageSize={onPageSize}
          />
        )}
      </div>
    </section>
  )
}

function ResultSummary({ result, busy }: { result: ResultState; busy: boolean }) {
  if (busy) {
    return (
      <span className="summary-chip">
        <span className="spinner" aria-hidden="true" />
        Ejecutando…
      </span>
    )
  }
  if (result.status === 'error') {
    return (
      <Badge tone="danger" size="xs">
        {result.error.errorType}
      </Badge>
    )
  }
  if (result.status !== 'success') return null
  const { response } = result
  return (
    <span className="summary-chip">
      <StatementBadge statement={response.statement} />
      {response.columns.length > 0 ? `${fmtInt(response.total_rows)} ${response.total_rows === 1 ? 'fila' : 'filas'}` : null}
      <span className="summary-sep" aria-hidden="true" />
      {fmtMs(response.metrics.total_ms)} ms
    </span>
  )
}

// ---------------------------------------------------------------------------

interface SuccessViewProps {
  response: QueryResponse
  script: StatementSummary[]
  canPaginate: boolean
  busy: boolean
  pageSize: number
  tables: readonly TableInfo[]
  onPage(page: number): void
  onPageSize(size: number): void
}

function SuccessView({ response, script, canPaginate, busy, pageSize, tables, onPage, onPageSize }: SuccessViewProps) {
  const hasGrid = response.columns.length > 0
  return (
    <div className="success-view">
      {script.length > 1 && <ScriptSummary statements={script} />}
      {hasGrid ? (
        <>
          <DataGrid response={response} tables={tables} />
          <Pagination
            response={response}
            pageSize={pageSize}
            canPaginate={canPaginate}
            busy={busy}
            onPage={onPage}
            onPageSize={onPageSize}
          />
        </>
      ) : (
        <MessageView response={response} />
      )}
    </div>
  )
}

function MessageView({ response }: { response: QueryResponse }) {
  const { metrics } = response
  return (
    <div className="message-view">
      <Icon name="checkCircle" size={26} className="message-icon" />
      <div className="message-content">
        <div className="message-head">
          <StatementBadge statement={response.statement || 'OK'} size="sm" />
          <span>ejecutada correctamente</span>
        </div>
        <p className="message-text">{response.message || 'Sentencia ejecutada.'}</p>
        <p className="message-meta">
          {fmtInt(metrics.disk_reads)} lecturas · {fmtInt(metrics.disk_writes)} escrituras · {fmtMs(metrics.total_ms)} ms
        </p>
      </div>
    </div>
  )
}

function ScriptSummary({ statements }: { statements: StatementSummary[] }) {
  const totals = statements.reduce(
    (acc, s) => ({
      reads: acc.reads + s.metrics.disk_reads,
      writes: acc.writes + s.metrics.disk_writes,
      ms: acc.ms + s.metrics.total_ms,
    }),
    { reads: 0, writes: 0, ms: 0 },
  )
  return (
    <details className="script-summary" open>
      <summary>
        <Icon name="chevronRight" size={14} className="details-caret" />
        <span className="script-title">Script de {statements.length} sentencias</span>
        <span className="script-totals">
          {fmtInt(totals.reads)} lecturas · {fmtInt(totals.writes)} escrituras · {fmtMs(totals.ms)} ms en total
        </span>
      </summary>
      <div className="script-table-wrap">
        <table className="mini-table script-table">
          <thead>
            <tr>
              <th scope="col" className="num">#</th>
              <th scope="col">Sentencia</th>
              <th scope="col">Mensaje</th>
              <th scope="col" className="num">Lecturas</th>
              <th scope="col" className="num">Escrituras</th>
              <th scope="col" className="num">ms</th>
            </tr>
          </thead>
          <tbody>
            {statements.map((s, i) => (
              <tr key={i}>
                <td className="num muted">{i + 1}</td>
                <td className="script-sql">
                  <StatementBadge statement={s.statement} />
                  <code title={s.sql}>{s.sql}</code>
                </td>
                <td className="script-msg" title={s.message}>
                  {s.message}
                </td>
                <td className="num">{fmtInt(s.metrics.disk_reads)}</td>
                <td className="num">{fmtInt(s.metrics.disk_writes)}</td>
                <td className="num">{fmtMs(s.metrics.total_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
}

// ---------------------------------------------------------------------------

function columnLookup(response: QueryResponse, tables: readonly TableInfo[]): Map<string, ColumnInfo> {
  const table = response.plan ? tables.find((t) => t.name === response.plan?.table) : undefined
  return new Map((table?.columns ?? []).map((c) => [c.name, c]))
}

function isNumericColumn(rows: CellValue[][], index: number): boolean {
  let sawNumber = false
  for (const row of rows) {
    const value = row[index]
    if (value === null || value === undefined) continue
    if (typeof value !== 'number') return false
    sawNumber = true
  }
  return sawNumber
}

function DataGrid({ response, tables }: { response: QueryResponse; tables: readonly TableInfo[] }) {
  const { columns, rows } = response
  const offset = (response.page - 1) * response.page_size

  const meta = useMemo(() => {
    const lookup = columnLookup(response, tables)
    return columns.map((name, j) => {
      const info = lookup.get(name)
      const numeric = info ? info.type === 'INT' || info.type === 'FLOAT' : isNumericColumn(rows, j)
      return { name, info, numeric }
    })
  }, [response, tables, columns, rows])

  return (
    <div className="grid-wrap" tabIndex={0} role="region" aria-label="Tabla de resultados">
      <table className="data-grid">
        <thead>
          <tr>
            <th scope="col" className="rownum">
              #
            </th>
            {meta.map((col, j) => (
              <th key={j} scope="col" className={col.numeric ? 'num' : undefined}>
                <span className="th-name">
                  {col.info?.primary_key && <Icon name="key" size={11} className="th-key" />}
                  {col.name}
                </span>
                {col.info && <span className="th-type">{columnTypeLabel(col.info)}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td className="grid-empty" colSpan={columns.length + 1}>
                La consulta no devolvió filas.
              </td>
            </tr>
          ) : (
            rows.map((row, i) => (
              <tr key={offset + i}>
                <td className="rownum">{offset + i + 1}</td>
                {meta.map((col, j) => {
                  const cell = formatCell(row[j], col.info?.type)
                  const cls = [cell.isNull && 'is-null', (cell.numeric || col.numeric) && 'num'].filter(Boolean).join(' ')
                  return (
                    <td key={j} className={cls || undefined}>
                      {cell.text}
                    </td>
                  )
                })}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}

interface PaginationProps {
  response: QueryResponse
  pageSize: number
  canPaginate: boolean
  busy: boolean
  onPage(page: number): void
  onPageSize(size: number): void
}

function Pagination({ response, pageSize, canPaginate, busy, onPage, onPageSize }: PaginationProps) {
  const { page, total_rows: totalRows } = response
  const effectiveSize = Math.max(1, response.page_size)
  const totalPages = Math.max(1, Math.ceil(totalRows / effectiveSize))
  const first = totalRows === 0 ? 0 : (page - 1) * effectiveSize + 1
  const last = Math.min(page * effectiveSize, totalRows)
  const navDisabled = !canPaginate || busy
  const [draft, setDraft] = useState<string | null>(null)

  const commit = () => {
    const n = Number.parseInt(draft ?? '', 10)
    setDraft(null)
    if (!Number.isFinite(n)) return
    const target = Math.min(Math.max(n, 1), totalPages)
    if (target !== page) onPage(target)
  }

  return (
    <div className="pagination" title={canPaginate ? undefined : 'La paginación solo reenvía sentencias SELECT'}>
      <div className="pager" role="group" aria-label="Paginación">
        <button type="button" className="btn btn-ghost btn-icon" aria-label="Primera página" disabled={navDisabled || page <= 1} onClick={() => onPage(1)}>
          <Icon name="chevronsLeft" />
        </button>
        <button type="button" className="btn btn-ghost btn-icon" aria-label="Página anterior" disabled={navDisabled || page <= 1} onClick={() => onPage(page - 1)}>
          <Icon name="chevronLeft" />
        </button>
        <span className="pager-label">
          Página
          <input
            className="input pager-input"
            inputMode="numeric"
            aria-label="Número de página"
            value={draft ?? String(page)}
            disabled={navDisabled}
            onFocus={(e) => {
              setDraft(String(page))
              e.currentTarget.select()
            }}
            onChange={(e) => setDraft(e.target.value.replace(/[^0-9]/g, ''))}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') e.currentTarget.blur()
              if (e.key === 'Escape') {
                setDraft(String(page))
                e.currentTarget.blur()
              }
            }}
          />
          de {fmtInt(totalPages)}
        </span>
        <button type="button" className="btn btn-ghost btn-icon" aria-label="Página siguiente" disabled={navDisabled || page >= totalPages} onClick={() => onPage(page + 1)}>
          <Icon name="chevronRight" />
        </button>
        <button type="button" className="btn btn-ghost btn-icon" aria-label="Última página" disabled={navDisabled || page >= totalPages} onClick={() => onPage(totalPages)}>
          <Icon name="chevronsRight" />
        </button>
      </div>
      <span className="pager-range">
        Filas {fmtInt(first)}–{fmtInt(last)} de <strong>{fmtInt(totalRows)}</strong>
      </span>
      <label className="pager-size">
        Por página
        <select className="input select" value={pageSize} disabled={busy} onChange={(e) => onPageSize(Number(e.target.value))}>
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}

// ---------------------------------------------------------------------------

function ErrorView({ error, sql, isSelection }: { error: ApiError; sql: string; isSelection: boolean }) {
  const pos = error.position
  const excerpt = useMemo(() => {
    if (!pos) return null
    const offset = codePointOffsetToUtf16(sql, pos.offset)
    const { line, column } = lineColumnAt(sql, offset)
    const text = sql.split('\n')[line - 1] ?? ''
    // Relleno que respeta tabulaciones para que el ^ quede alineado
    const pad = text.slice(0, column - 1).replace(/[^\t]/g, ' ')
    return { line, text, pad }
  }, [pos, sql])

  const gutter = excerpt ? String(excerpt.line).length : 1

  return (
    <div className="error-view" role="alert">
      <div className="error-head">
        <Icon name="alert" size={18} />
        <span className="error-type">{error.errorType}</span>
        {error.status !== null && <span className="error-status">HTTP {error.status}</span>}
      </div>
      <p className="error-message">{error.message}</p>
      {pos && (
        <p className="error-pos">
          línea {pos.line}, columna {pos.column}
          {isSelection && <span className="muted"> · relativo a la selección ejecutada</span>}
        </p>
      )}
      {excerpt && (
        <pre className="error-excerpt" aria-label="Fragmento con el error">
          <span className="excerpt-gutter">{String(excerpt.line).padStart(gutter)} │ </span>
          {excerpt.text}
          {'\n'}
          <span className="excerpt-gutter">{' '.repeat(gutter)} │ </span>
          {excerpt.pad}
          <span className="excerpt-caret">^</span>
        </pre>
      )}
      {error.kind === 'network' && (
        <p className="error-hint">
          Levanta el backend (por ejemplo con <code>docker compose up --build</code>) y vuelve a ejecutar la consulta.
        </p>
      )}
    </div>
  )
}
