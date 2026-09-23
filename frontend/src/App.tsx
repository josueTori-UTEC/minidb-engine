import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  executeQuery,
  fetchHealth,
  fetchTables,
  reorganizeTable,
  toApiError,
  type PlannerMode,
  type QueryResponse,
  type StatementSummary,
  type TableInfo,
} from './api'
import { AppHeader, type HealthState, type ThemePreference } from './components/AppHeader'
import { PageInspector } from './components/PageInspector'
import { PlanPanel, type HistoryView } from './components/PlanPanel'
import { PAGE_SIZES, ResultsPanel } from './components/ResultsPanel'
import { SqlEditor, type RunTarget, type SqlEditorHandle } from './components/SqlEditor'
import { TableExplorer, type CatalogStatus, type PlanHighlight } from './components/TableExplorer'
import { codePointOffsetToUtf16 } from './lib/format'
import { STORAGE_KEYS, readJson, readString, writeJson, writeString } from './lib/storage'
import { HISTORY_LIMIT, type Activity, type HistoryEntry, type HistoryState, type ResultState } from './types'

const DEFAULT_PAGE_SIZE = 50
const HEALTH_INTERVAL_MS = 15_000

type NewHistoryEntry = Omit<HistoryEntry, 'id'>

function loadPageSize(): number {
  const n = Number(readString(STORAGE_KEYS.pageSize))
  return (PAGE_SIZES as readonly number[]).includes(n) ? n : DEFAULT_PAGE_SIZE
}

function loadTheme(): ThemePreference {
  const value = readString(STORAGE_KEYS.theme)
  return value === 'light' || value === 'dark' ? value : 'system'
}

function isHistoryEntry(value: unknown): value is HistoryEntry {
  if (typeof value !== 'object' || value === null) return false
  const e = value as Record<string, unknown>
  return (
    typeof e.id === 'number' &&
    typeof e.kind === 'string' &&
    typeof e.sql === 'string' &&
    typeof e.reads === 'number' &&
    typeof e.writes === 'number' &&
    typeof e.totalMs === 'number' &&
    (e.estimated === null || typeof e.estimated === 'number')
  )
}

function isHistoryState(value: unknown): value is HistoryState {
  if (typeof value !== 'object' || value === null) return false
  const h = value as Record<string, unknown>
  return typeof h.next === 'number' && Array.isArray(h.items) && h.items.every(isHistoryEntry)
}

/** Una entrada por sentencia ejecutada; el plan (y su estimación) corresponde a la última. */
function historyFromResponse(res: QueryResponse, page?: number): NewHistoryEntry[] {
  const statements: StatementSummary[] =
    res.statements.length > 0
      ? res.statements
      : [{ sql: '', statement: res.statement, message: res.message, metrics: res.metrics }]
  return statements.map((s, i) => {
    const isLast = i === statements.length - 1
    return {
      kind: isLast && res.plan ? res.plan.type : s.statement || 'SQL',
      sql: s.sql,
      reads: s.metrics.disk_reads,
      writes: s.metrics.disk_writes,
      estimated: isLast ? (res.plan?.estimated_io ?? null) : null,
      totalMs: s.metrics.total_ms,
      page,
    }
  })
}

interface ExecuteOptions {
  page: number
  pageSize: number
  /** Presente cuando la ejecución viene del editor (para ubicar errores). */
  target?: RunTarget
  /** Presente cuando la ejecución es un cambio de página: se conserva el script original. */
  script?: StatementSummary[]
}

export default function App() {
  const editorRef = useRef<SqlEditorHandle>(null)

  // --- Editor (persistido en localStorage) ---
  const [sqlText, setSqlText] = useState(() => readString(STORAGE_KEYS.editor) ?? '')
  const sqlTextRef = useRef(sqlText)
  useEffect(() => {
    sqlTextRef.current = sqlText
    const timer = window.setTimeout(() => writeString(STORAGE_KEYS.editor, sqlText), 250)
    return () => window.clearTimeout(timer)
  }, [sqlText])
  useEffect(() => {
    const flush = () => writeString(STORAGE_KEYS.editor, sqlTextRef.current)
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [])

  // --- Modo del planificador (persistido) ---
  const [planner, setPlanner] = useState<PlannerMode>(() =>
    readString(STORAGE_KEYS.planner) === 'cost' ? 'cost' : 'rules',
  )
  const plannerRef = useRef(planner)
  useEffect(() => {
    plannerRef.current = planner
    writeString(STORAGE_KEYS.planner, planner)
  }, [planner])

  // --- Estado del backend ---
  const [health, setHealth] = useState<HealthState>({ status: 'checking' })
  const checkHealth = useCallback(async () => {
    try {
      const signal = typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(5000) : undefined
      const info = await fetchHealth(signal)
      setHealth({ status: 'online', info })
    } catch (err) {
      setHealth({ status: 'offline', message: toApiError(err).message })
    }
  }, [])
  const markOffline = useCallback((message: string) => setHealth({ status: 'offline', message }), [])

  useEffect(() => {
    void checkHealth()
    const timer = window.setInterval(() => void checkHealth(), HEALTH_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [checkHealth])

  // --- Catálogo ---
  const [tables, setTables] = useState<TableInfo[]>([])
  const [catalog, setCatalog] = useState<{ status: CatalogStatus; error: string | null }>({ status: 'loading', error: null })
  const tablesSeq = useRef(0)
  const loadTables = useCallback(async () => {
    const seq = ++tablesSeq.current
    setCatalog((prev) => ({ status: 'loading', error: prev.error }))
    try {
      const list = await fetchTables()
      if (seq !== tablesSeq.current) return
      setTables(list)
      setCatalog({ status: 'ready', error: null })
    } catch (err) {
      if (seq !== tablesSeq.current) return
      const error = toApiError(err)
      setCatalog({ status: 'error', error: error.message })
      if (error.kind === 'network') markOffline(error.message)
    }
  }, [markOffline])

  useEffect(() => {
    void loadTables()
  }, [loadTables])

  // Si el backend vuelve a estar en línea, se recarga el catálogo
  const lastHealth = useRef(health.status)
  useEffect(() => {
    if (lastHealth.current === 'offline' && health.status === 'online') void loadTables()
    lastHealth.current = health.status
  }, [health.status, loadTables])

  // --- Historial de I/O ---
  const [history, setHistory] = useState<HistoryState>(
    () => readJson(STORAGE_KEYS.history, isHistoryState) ?? { next: 1, items: [] },
  )
  useEffect(() => writeJson(STORAGE_KEYS.history, history), [history])
  const appendHistory = useCallback((entries: NewHistoryEntry[]) => {
    if (entries.length === 0) return
    setHistory((prev) => {
      const items = [...prev.items, ...entries.map((entry, i) => ({ ...entry, id: prev.next + i }))]
      return { next: prev.next + entries.length, items: items.slice(-HISTORY_LIMIT) }
    })
  }, [])
  const clearHistory = useCallback(() => setHistory((prev) => ({ next: prev.next, items: [] })), [])

  const [logScale, setLogScale] = useState(() => readString(STORAGE_KEYS.chartScale) === '1')
  useEffect(() => writeString(STORAGE_KEYS.chartScale, logScale ? '1' : '0'), [logScale])
  const [historyView, setHistoryView] = useState<HistoryView>('chart')

  // --- Ejecución de SQL ---
  const [pageSize, setPageSize] = useState(loadPageSize)
  const [result, setResult] = useState<ResultState>({ status: 'idle' })
  const [activity, setActivity] = useState<Activity | null>(null)
  const [busy, setBusy] = useState(false)
  const [reorganizing, setReorganizing] = useState<string | null>(null)
  const [inspecting, setInspecting] = useState<string | null>(null)
  const querySeq = useRef(0)
  const locked = busy || reorganizing !== null

  const execute = useCallback(
    async (sql: string, options: ExecuteOptions) => {
      const seq = ++querySeq.current
      setBusy(true)
      try {
        const res = await executeQuery({
          sql,
          page: options.page,
          page_size: options.pageSize,
          planner: plannerRef.current,
        })
        if (seq !== querySeq.current) return
        const isPaging = options.script !== undefined
        // Al paginar solo se reenvía el último SELECT, nunca las sentencias que lo precedían en el script
        const last = res.statements[res.statements.length - 1]
        const pageSql =
          res.statement !== 'SELECT' ? null : isPaging || res.statements.length <= 1 ? sql : (last?.sql ?? null)
        setResult({ status: 'success', response: res, script: options.script ?? res.statements, pageSql })
        setActivity({ kind: 'query', response: res })
        appendHistory(historyFromResponse(res, isPaging ? res.page : undefined))
        if (lastHealth.current !== 'online') void checkHealth()

        const kinds = res.statements.length > 0 ? res.statements.map((s) => s.statement) : [res.statement]
        if (!isPaging && kinds.some((kind) => kind !== 'SELECT')) void loadTables()
      } catch (err) {
        if (seq !== querySeq.current) return
        const error = toApiError(err)
        if (error.kind === 'network') markOffline(error.message)
        setResult({ status: 'error', error, sql, isSelection: options.target?.isSelection ?? false })
        setActivity({ kind: 'error', action: 'ejecución', error })
        if (options.target && error.position) {
          editorRef.current?.revealError(sql, options.target.base, codePointOffsetToUtf16(sql, error.position.offset))
        }
        // Un script puede fallar a la mitad después de modificar tablas
        if (error.kind === 'http' && options.script === undefined) void loadTables()
      } finally {
        if (seq === querySeq.current) setBusy(false)
      }
    },
    [appendHistory, checkHealth, loadTables, markOffline],
  )

  const runFromEditor = useCallback(
    (target: RunTarget) => {
      if (locked) return
      void execute(target.sql, { page: 1, pageSize, target })
    },
    [execute, locked, pageSize],
  )

  const changePage = (page: number) => {
    if (locked || result.status !== 'success' || result.pageSql === null) return
    void execute(result.pageSql, { page, pageSize, script: result.script })
  }

  const changePageSize = (size: number) => {
    setPageSize(size)
    writeString(STORAGE_KEYS.pageSize, String(size))
    if (locked || result.status !== 'success' || result.pageSql === null) return
    // Mantener visible la primera fila de la página actual
    const firstRow = (result.response.page - 1) * result.response.page_size
    void execute(result.pageSql, { page: Math.floor(firstRow / size) + 1, pageSize: size, script: result.script })
  }

  // Ctrl/Cmd+Enter también funciona con el foco fuera del editor
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.defaultPrevented) return
      if (inspecting !== null) return
      event.preventDefault()
      editorRef.current?.run()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [inspecting])

  // --- Reorganización de tablas SEQUENTIAL ---
  const reorganize = useCallback(
    async (table: string) => {
      if (locked) return
      setReorganizing(table)
      try {
        const res = await reorganizeTable(table)
        setActivity({ kind: 'reorganize', result: res })
        appendHistory([
          {
            kind: 'REORGANIZE',
            sql: `Reorganizar ${table}`,
            reads: res.metrics.disk_reads,
            writes: res.metrics.disk_writes,
            estimated: null,
            totalMs: res.metrics.total_ms,
          },
        ])
      } catch (err) {
        const error = toApiError(err)
        if (error.kind === 'network') markOffline(error.message)
        setActivity({ kind: 'error', action: `reorganización de ${table}`, error })
      } finally {
        setReorganizing(null)
        void loadTables()
      }
    },
    [appendHistory, loadTables, locked, markOffline],
  )

  // --- Tema ---
  const [theme, setTheme] = useState<ThemePreference>(loadTheme)
  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') delete root.dataset.theme
    else root.dataset.theme = theme
    writeString(STORAGE_KEYS.theme, theme)
  }, [theme])

  const highlight = useMemo<PlanHighlight | null>(() => {
    if (activity?.kind === 'query' && activity.response.plan) {
      return { table: activity.response.plan.table, index: activity.response.plan.index }
    }
    if (activity?.kind === 'reorganize') return { table: activity.result.table, index: null }
    return null
  }, [activity])

  return (
    <div className="app">
      <AppHeader health={health} theme={theme} onTheme={setTheme} onRecheck={() => void checkHealth()} />
      <main className="workspace">
        <TableExplorer
          tables={tables}
          status={catalog.status}
          error={catalog.error}
          locked={locked}
          reorganizing={reorganizing}
          highlight={highlight}
          onRefresh={() => void loadTables()}
          onPickTable={(name) => editorRef.current?.insertSnippet(`SELECT * FROM ${name};`)}
          onReorganize={(name) => void reorganize(name)}
          onInspect={setInspecting}
        />
        <SqlEditor
          ref={editorRef}
          value={sqlText}
          onChange={setSqlText}
          onRun={runFromEditor}
          running={busy}
          locked={locked}
          tables={tables}
          planner={planner}
          onPlanner={setPlanner}
        />
        <ResultsPanel
          result={result}
          busy={busy}
          pageSize={pageSize}
          tables={tables}
          onPage={changePage}
          onPageSize={changePageSize}
        />
        <PlanPanel
          activity={activity}
          busy={busy || reorganizing !== null}
          history={history.items}
          logScale={logScale}
          historyView={historyView}
          onLogScale={setLogScale}
          onHistoryView={setHistoryView}
          onClearHistory={clearHistory}
        />
      </main>
      {inspecting !== null && (
        <PageInspector
          table={inspecting}
          tableInfo={tables.find((t) => t.name === inspecting)}
          onClose={() => setInspecting(null)}
        />
      )}
    </div>
  )
}
