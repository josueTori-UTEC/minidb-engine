import { Fragment } from 'react'
import type {
  IoBreakdown,
  Metrics,
  Plan,
  PlanCandidate,
  QueryResponse,
  ReorganizeResponse,
  ReorganizeSnapshot,
  SpatialPlan,
  TextPlan,
  VectorPlan,
} from '../api'
import { fmtInt, fmtMs, fmtNumber, formatPoint, plural } from '../lib/format'
import { HISTORY_LIMIT, type Activity, type HistoryEntry } from '../types'
import { Badge, PLAN_DESCRIPTION, PlanBadge, STRUCTURE_DESCRIPTION, StatementBadge, StructureBadge } from './Badges'
import { HistoryTable, IoHistoryChart } from './IoHistoryChart'
import { Icon } from './Icon'

export type HistoryView = 'chart' | 'table'

interface PlanPanelProps {
  activity: Activity | null
  busy: boolean
  history: readonly HistoryEntry[]
  logScale: boolean
  historyView: HistoryView
  onLogScale(value: boolean): void
  onHistoryView(view: HistoryView): void
  onClearHistory(): void
}

export function PlanPanel(props: PlanPanelProps) {
  const { activity, busy } = props
  return (
    <section className="panel panel-plan" aria-labelledby="plan-title" aria-busy={busy}>
      <header className="panel-header">
        <h2 className="panel-title" id="plan-title">
          <Icon name="plan" />
          Plan de ejecución y métricas
        </h2>
      </header>
      <div className={`plan-scroll${busy ? ' is-busy' : ''}`}>
        {activity === null && (
          <div className="empty-state compact">
            <Icon name="plan" size={26} className="empty-icon" />
            <p className="empty-title">Sin ejecuciones</p>
            <p className="empty-text">
              Aquí verás el plan elegido por el optimizador, los candidatos con su costo estimado y las lecturas y
              escrituras reales en disco.
            </p>
          </div>
        )}
        {activity?.kind === 'query' && <QueryActivity response={activity.response} />}
        {activity?.kind === 'reorganize' && <ReorganizeActivity result={activity.result} />}
        {activity?.kind === 'error' && (
          <div className="plan-hero">
            <Badge tone="danger" size="lg">
              {activity.error.errorType}
            </Badge>
            <p className="plan-desc">
              La {activity.action} terminó con error, así que no hay plan que mostrar.
            </p>
            <p className="plan-error">{activity.error.message}</p>
          </div>
        )}
      </div>
      <HistorySection {...props} />
    </section>
  )
}

// ---------------------------------------------------------------------------

function QueryActivity({ response }: { response: QueryResponse }) {
  const { plan, metrics } = response
  return (
    <>
      {plan ? (
        <PlanDetails plan={plan} />
      ) : (
        <div className="plan-hero">
          <Badge tone="neutral" size="lg">
            Sin plan
          </Badge>
          <p className="plan-desc">
            <StatementBadge statement={response.statement} /> no pasa por el planificador. Igual se miden sus lecturas y
            escrituras en disco.
          </p>
        </div>
      )}
      <MetricTiles metrics={metrics} />
      {metrics.io && <IoByFile io={metrics.io} />}
      {plan && <EstimateVsActual estimated={plan.estimated_io} metrics={metrics} />}
      {plan?.spatial && <SpatialSection spatial={plan.spatial} />}
      {plan?.text && <TextSection text={plan.text} />}
      {plan?.vector && <VectorSection vector={plan.vector} />}
      {plan && plan.candidates.length > 0 && <Candidates candidates={plan.candidates} />}
    </>
  )
}

const STRATEGY_INFO: Record<string, { label: string; note: string }> = {
  'pre-filter': {
    label: 'Pre-filtering',
    note: 'primero el filtro escalar; el índice solo evalúa los registros que lo cumplen',
  },
  'post-filter': {
    label: 'Post-filtering',
    note: 'primero la búsqueda en el índice; el filtro escalar se aplica a sus candidatos',
  },
}

/** Etiqueta visible de la estrategia de una consulta híbrida. */
export function StrategyBadge({ strategy, size = 'sm' }: { strategy: string; size?: 'xs' | 'sm' }) {
  const info = STRATEGY_INFO[strategy]
  return (
    <Badge tone={strategy === 'pre-filter' ? 'prefilter' : 'postfilter'} size={size} title={info?.note}>
      {info?.label ?? strategy}
    </Badge>
  )
}

export function PlanWarning({ message }: { message: string }) {
  return (
    <div className="inline-alert is-warn" role="status">
      <Icon name="alert" size={14} />
      <span>{message}</span>
    </div>
  )
}

function PlanDetails({ plan }: { plan: Plan }) {
  return (
    <>
      <div className="plan-hero">
        <PlanBadge type={plan.type} size="lg" />
        <p className="plan-desc">{PLAN_DESCRIPTION[plan.type] ?? 'Plan reportado por el backend.'}</p>
      </div>
      {plan.warning && <PlanWarning message={plan.warning} />}
      <dl className="kv">
        <dt>Tabla</dt>
        <dd>
          <code>{plan.table || '—'}</code>
        </dd>
        <dt>Estructura</dt>
        <dd>
          {plan.structure ? <StructureBadge structure={plan.structure} /> : '—'}
          {STRUCTURE_DESCRIPTION[plan.structure] && <span className="kv-note">{STRUCTURE_DESCRIPTION[plan.structure]}</span>}
        </dd>
        <dt>Índice</dt>
        <dd>{plan.index ? <code>{plan.index}</code> : <span className="muted">ninguno</span>}</dd>
        <dt>Columna</dt>
        <dd>{plan.column ? <code>{plan.column}</code> : <span className="muted">—</span>}</dd>
        <dt>Predicado</dt>
        <dd>{plan.predicate ? <code className="code-chip">{plan.predicate}</code> : <span className="muted">sin predicado</span>}</dd>
        <dt>Filtro residual</dt>
        <dd>{plan.filter ? <code className="code-chip">{plan.filter}</code> : <span className="muted">ninguno</span>}</dd>
        {plan.strategy && (
          <>
            <dt>Estrategia</dt>
            <dd>
              <StrategyBadge strategy={plan.strategy} />
              {STRATEGY_INFO[plan.strategy] && <span className="kv-note">{STRATEGY_INFO[plan.strategy]?.note}</span>}
              {plan.prefilter_rids != null && (
                <span className="kv-note">· {plural(plan.prefilter_rids, 'RID candidato', 'RID candidatos')} del filtro</span>
              )}
            </dd>
          </>
        )}
        {plan.order_by && (
          <>
            <dt>Orden</dt>
            <dd>
              <code className="code-chip">{plan.order_by}</code>
            </dd>
          </>
        )}
        {plan.limit != null && (
          <>
            <dt>Límite</dt>
            <dd>{plural(plan.limit, 'fila', 'filas')}</dd>
          </>
        )}
        {plan.planner && (
          <>
            <dt>Planificador</dt>
            <dd>{plan.planner === 'cost' ? 'por costos (menor I/O estimado)' : 'por reglas (enunciado)'}</dd>
          </>
        )}
        {plan.detail && (
          <>
            <dt>Detalle</dt>
            <dd className="kv-detail">{plan.detail}</dd>
          </>
        )}
      </dl>
    </>
  )
}

function MetricTiles({ metrics }: { metrics: Metrics }) {
  return (
    <div className="kpi-grid" role="group" aria-label="Métricas de la sentencia">
      <div className="kpi kpi-io">
        <span className="kpi-label">
          <span className="series-key key-reads" aria-hidden="true" />
          Lecturas de disco
        </span>
        <span className="kpi-value">
          {fmtInt(metrics.disk_reads)}
          <span className="kpi-unit">págs.</span>
        </span>
      </div>
      <div className="kpi kpi-io">
        <span className="kpi-label">
          <span className="series-key key-writes" aria-hidden="true" />
          Escrituras de disco
        </span>
        <span className="kpi-value">
          {fmtInt(metrics.disk_writes)}
          <span className="kpi-unit">págs.</span>
        </span>
      </div>
      <TimeTile label="Parse" value={metrics.parse_ms} />
      <TimeTile label="Ejecución" value={metrics.exec_ms} />
      <TimeTile label="Total" value={metrics.total_ms} />
    </div>
  )
}

function TimeTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="kpi kpi-time">
      <span className="kpi-label">{label}</span>
      <span className="kpi-value">
        {fmtMs(value)}
        <span className="kpi-unit">ms</span>
      </span>
    </div>
  )
}

const ROLE_LABEL: Record<string, string> = { index: 'índice', table: 'tabla', other: 'otro' }
const ROLE_ORDER: Record<string, number> = { index: 0, table: 1, other: 2 }

/** Desglose de las páginas leídas y escritas: cuánto se fue en índices y cuánto en la tabla de datos. */
function IoByFile({ io }: { io: IoBreakdown }) {
  const groups = [
    { key: 'index', label: 'Índice', reads: io.index_reads, writes: io.index_writes },
    { key: 'table', label: 'Tabla', reads: io.table_reads, writes: io.table_writes },
    { key: 'other', label: 'Otros', reads: io.other_reads, writes: io.other_writes },
  ].filter((g) => g.key !== 'other' || g.reads + g.writes > 0)
  const max = Math.max(1, ...groups.map((g) => g.reads))
  const files = [...io.files]
    .filter((f) => f.reads + f.writes > 0)
    .sort((a, b) => (ROLE_ORDER[a.role] ?? 3) - (ROLE_ORDER[b.role] ?? 3) || b.reads + b.writes - (a.reads + a.writes))

  return (
    <div className="plan-section">
      <div className="section-head">
        <h3 className="section-title">I/O por archivo</h3>
        <span className="section-hint">índice vs. tabla de datos</span>
      </div>
      <div className="compare-rows">
        {groups.map((g) => (
          <Fragment key={g.key}>
            <span className="compare-label">{g.label}</span>
            <div className="compare-track">
              {g.reads > 0 && <span className={`compare-bar bar-${g.key} is-end`} style={{ width: `${(g.reads / max) * 100}%` }} />}
            </div>
            <span className="compare-value io-value">
              {plural(g.reads, 'lectura', 'lecturas')}
              {g.writes > 0 && <span className="muted"> · {fmtInt(g.writes)} escr.</span>}
            </span>
          </Fragment>
        ))}
      </div>
      {files.length > 0 && (
        <ul className="io-files">
          {files.map((f) => (
            <li key={f.file}>
              <span className={`io-role role-${f.role}`}>{ROLE_LABEL[f.role] ?? f.role}</span>
              <span className="io-file" title={`${f.file} · ${f.owner}`}>
                {f.file}
              </span>
              <span className="io-counts">
                {fmtInt(f.reads)} L{f.writes > 0 && ` · ${fmtInt(f.writes)} E`}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function SpatialSection({ spatial }: { spatial: SpatialPlan }) {
  const center = spatial.center ? formatPoint(spatial.center) : null
  let query: string
  if (spatial.kind === 'radius') {
    query = `radio de ${spatial.radius_km != null ? fmtNumber(spatial.radius_km, 3) : '—'} km${center ? ` alrededor de ${center}` : ''}`
  } else if (spatial.kind === 'box') {
    query = `rectángulo ${spatial.box ? formatPoint(spatial.box) : ''}`
  } else if (spatial.kind === 'knn') {
    query = `${spatial.k != null ? `${fmtInt(spatial.k)} ` : ''}vecinos más cercanos${center ? ` a ${center}` : ''}`
  } else {
    query = String(spatial.kind)
  }
  return (
    <div className="plan-section">
      <div className="section-head">
        <h3 className="section-title">Búsqueda espacial</h3>
        <span className="section-hint">R-Tree</span>
      </div>
      <dl className="kv">
        <dt>Consulta</dt>
        <dd>{query}</dd>
        {spatial.nodes_visited != null && (
          <>
            <dt>Nodos visitados</dt>
            <dd>
              {fmtInt(spatial.nodes_visited)}
              {spatial.leaves_visited != null && <span className="kv-note">{plural(spatial.leaves_visited, 'hoja', 'hojas')}</span>}
            </dd>
          </>
        )}
        {spatial.candidates != null && (
          <>
            <dt>Candidatos</dt>
            <dd>{plural(spatial.candidates, 'punto', 'puntos')}</dd>
          </>
        )}
      </dl>
      {(spatial.mbrs?.length ?? 0) > 0 && (
        <p className="section-note">
          Los {fmtInt(spatial.mbrs?.length)} MBR inspeccionados se pueden dibujar en la vista Mapa de los resultados.
        </p>
      )}
    </div>
  )
}

function TextSection({ text }: { text: TextPlan }) {
  const terms = text.terms ?? []
  return (
    <div className="plan-section">
      <div className="section-head">
        <h3 className="section-title">Búsqueda de texto</h3>
        <span className="section-hint">
          {text.mode === 'boolean' ? 'consulta booleana' : text.mode === 'ranked' ? 'ranking por similitud' : text.mode}
          {text.scoring && ` · ${text.scoring}`}
        </span>
      </div>
      <dl className="kv">
        {text.matched != null && (
          <>
            <dt>Coincidencias</dt>
            <dd>{plural(text.matched, 'documento', 'documentos')}</dd>
          </>
        )}
        {text.rebuilt && (
          <>
            <dt>Índice</dt>
            <dd className="kv-detail">se reconstruyó antes de consultar (estaba desactualizado)</dd>
          </>
        )}
      </dl>
      {terms.length > 0 && (
        <div className="plan-table-wrap">
          <table className="mini-table">
            <thead>
              <tr>
                <th scope="col">Término</th>
                <th scope="col" className="num" title="Documentos que contienen el término">df</th>
                <th scope="col" className="num" title="Frecuencia inversa de documento">idf</th>
                <th scope="col" className="num" title="Páginas de la lista de posteo">Págs. posteo</th>
              </tr>
            </thead>
            <tbody>
              {terms.map((t) => (
                <tr key={t.term}>
                  <td className="mono">{t.term}</td>
                  <td className="num">{fmtInt(t.df)}</td>
                  <td className="num">{typeof t.idf === 'number' ? t.idf.toFixed(4) : '—'}</td>
                  <td className="num">{fmtInt(t.posting_pages)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function VectorSection({ vector }: { vector: VectorPlan }) {
  const probed = vector.probed ?? []
  return (
    <div className="plan-section">
      <div className="section-head">
        <h3 className="section-title">Búsqueda vectorial</h3>
        <span className="section-hint">IVF{vector.metric && ` · ${vector.metric}`}</span>
      </div>
      <dl className="kv">
        {vector.probes != null && (
          <>
            <dt>Celdas exploradas</dt>
            <dd>
              {fmtInt(vector.probes)}
              {vector.lists != null && <span className="kv-note">de {fmtInt(vector.lists)} (ivf.probes)</span>}
            </dd>
          </>
        )}
        {(vector.dir_pages != null || vector.list_pages != null) && (
          <>
            <dt>Páginas del índice</dt>
            <dd>
              {fmtInt(vector.dir_pages ?? 0)} de centroides · {fmtInt(vector.list_pages ?? 0)} de listas
            </dd>
          </>
        )}
        {vector.scanned != null && (
          <>
            <dt>Vectores comparados</dt>
            <dd>{fmtInt(vector.scanned)}</dd>
          </>
        )}
        {vector.fetched != null && (
          <>
            <dt>Registros leídos</dt>
            <dd>{fmtInt(vector.fetched)}</dd>
          </>
        )}
        {vector.rebuilt && (
          <>
            <dt>Índice</dt>
            <dd className="kv-detail">se reconstruyó antes de consultar</dd>
          </>
        )}
      </dl>
      {probed.length > 0 && (
        <div className="chip-list" aria-label="Celdas exploradas">
          {probed.slice(0, 60).map((id) => (
            <span key={id} className="chip" title={`Celda ${id}`}>
              {id}
            </span>
          ))}
          {probed.length > 60 && <span className="chip chip-more">+{fmtInt(probed.length - 60)} más</span>}
        </div>
      )}
    </div>
  )
}

function EstimateVsActual({ estimated, metrics }: { estimated: number | null; metrics: Metrics }) {
  const reads = metrics.disk_reads
  const writes = metrics.disk_writes
  const actual = reads + writes
  const max = Math.max(actual, estimated ?? 0, 1)
  const pct = (v: number) => `${(v / max) * 100}%`

  let verdict: { text: string; exact: boolean }
  if (estimated === null) verdict = { text: 'el plan no reportó estimación', exact: false }
  else if (actual === estimated) verdict = { text: 'coincide con lo estimado', exact: true }
  else if (estimated === 0) verdict = { text: `${fmtInt(actual)} I/O sobre un estimado de 0`, exact: false }
  else {
    const diff = ((actual - estimated) / estimated) * 100
    const rounded = Math.abs(diff) < 10 ? diff.toFixed(1) : Math.round(diff).toString()
    verdict = {
      text: `real ${diff > 0 ? '+' : ''}${rounded} % ${diff > 0 ? 'sobre' : 'bajo'} lo estimado`,
      exact: false,
    }
  }

  return (
    <div className="plan-section">
      <div className="section-head">
        <h3 className="section-title">I/O estimado vs real</h3>
        <span className={`verdict${verdict.exact ? ' is-exact' : ''}`}>
          {verdict.exact && <Icon name="check" size={13} />}
          {verdict.text}
        </span>
      </div>
      <div className="compare-rows">
        <span className="compare-label">Estimado</span>
        <div className="compare-track">
          {estimated !== null && estimated > 0 && <span className="compare-bar bar-est is-end" style={{ width: pct(estimated) }} />}
        </div>
        <span className="compare-value">{estimated === null ? '—' : fmtInt(estimated)}</span>

        <span className="compare-label">Real</span>
        <div className="compare-track">
          {reads > 0 && <span className={`compare-bar bar-reads${writes > 0 ? '' : ' is-end'}`} style={{ width: pct(reads) }} />}
          {writes > 0 && <span className={`compare-bar bar-writes is-end${reads > 0 ? ' has-gap' : ''}`} style={{ width: pct(writes) }} />}
        </div>
        <span className="compare-value">{fmtInt(actual)}</span>
      </div>
      <p className="section-note">Real = lecturas + escrituras de páginas medidas por el DiskCounter.</p>
    </div>
  )
}

function Candidates({ candidates }: { candidates: PlanCandidate[] }) {
  const max = Math.max(1, ...candidates.map((c) => c.estimated_io ?? 0))
  const logWidth = (v: number) => (v <= 0 ? 0 : Math.max(3, (Math.log10(v + 1) / Math.log10(max + 1)) * 100))
  return (
    <div className="plan-section">
      <div className="section-head">
        <h3 className="section-title">Planes candidatos</h3>
        <span className="section-hint">costo estimado · escala log</span>
      </div>
      <ul className="candidate-list">
        {candidates.map((c, i) => (
          <li key={i} className={c.chosen ? 'is-chosen' : undefined}>
            <span className="cand-mark" aria-label={c.chosen ? 'elegido' : undefined}>
              {c.chosen && <Icon name="check" size={13} />}
            </span>
            <span className="cand-name">
              <PlanBadge type={c.type} size="xs" />
              <span className="cand-access">
                {c.index ?? c.structure}
                {c.index && <span className="muted"> · {c.structure}</span>}
                {c.strategy && <span className="muted"> · {STRATEGY_INFO[c.strategy]?.label ?? c.strategy}</span>}
              </span>
            </span>
            <span className="cand-track" aria-hidden="true">
              {c.estimated_io !== null && c.estimated_io > 0 && (
                <span className="cand-bar" style={{ width: `${logWidth(c.estimated_io)}%` }} />
              )}
            </span>
            <span className="cand-value">{c.estimated_io === null ? '—' : fmtInt(c.estimated_io)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ReorganizeActivity({ result }: { result: ReorganizeResponse }) {
  const rows: { label: string; key: keyof ReorganizeSnapshot }[] = [
    { label: 'Páginas principales', key: 'main_pages' },
    { label: 'Páginas de overflow', key: 'overflow_pages' },
    { label: 'Registros', key: 'record_count' },
  ]
  return (
    <>
      <div className="plan-hero">
        <Badge tone="reorg" size="lg">
          Reorganización
        </Badge>
        <p className="plan-desc">
          <code>{result.table}</code> · {result.message}
        </p>
      </div>
      <table className="mini-table compare-table">
        <thead>
          <tr>
            <th scope="col" />
            <th scope="col" className="num">Antes</th>
            <th scope="col" className="num">Después</th>
            <th scope="col" className="num">Δ</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ label, key }) => {
            const before = result.before?.[key] ?? 0
            const after = result.after?.[key] ?? 0
            const delta = after - before
            return (
              <tr key={key}>
                <th scope="row">{label}</th>
                <td className="num">{fmtInt(before)}</td>
                <td className="num">{fmtInt(after)}</td>
                <td className="num muted">{delta === 0 ? '=' : `${delta > 0 ? '+' : '−'}${fmtInt(Math.abs(delta))}`}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <MetricTiles metrics={result.metrics} />
    </>
  )
}

// ---------------------------------------------------------------------------

function HistorySection({ history, logScale, historyView, onLogScale, onHistoryView, onClearHistory }: PlanPanelProps) {
  return (
    <div className="history-section">
      <div className="history-head">
        <h3 className="section-title">
          Historial de I/O
          <span className="section-hint">
            {history.length}/{HISTORY_LIMIT}
          </span>
        </h3>
        <div className="history-controls">
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            aria-pressed={logScale}
            onClick={() => onLogScale(!logScale)}
            disabled={historyView !== 'chart'}
            title="Escala logarítmica en el eje Y (el I/O va de 1 a más de 100k)"
          >
            <span className={`toggle-dot${logScale ? ' is-on' : ''}`} aria-hidden="true" />
            Log
          </button>
          <div className="segmented" role="group" aria-label="Vista del historial">
            <button type="button" aria-pressed={historyView === 'chart'} onClick={() => onHistoryView('chart')} title="Gráfico">
              <Icon name="barChart" size={14} />
              <span className="sr-only">Gráfico</span>
            </button>
            <button type="button" aria-pressed={historyView === 'table'} onClick={() => onHistoryView('table')} title="Tabla">
              <Icon name="list" size={14} />
              <span className="sr-only">Tabla</span>
            </button>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-xs"
            onClick={onClearHistory}
            disabled={history.length === 0}
            title="Vaciar historial"
            aria-label="Vaciar historial"
          >
            <Icon name="eraser" size={14} />
          </button>
        </div>
      </div>
      {historyView === 'chart' && history.length > 0 && (
        <div className="chart-legend">
          <span>
            <span className="legend-swatch key-reads" />
            Lecturas
          </span>
          <span>
            <span className="legend-swatch key-writes" />
            Escrituras
          </span>
          <span>
            <span className="legend-dot key-est" />
            I/O estimado
          </span>
          {logScale && <span className="legend-note">eje Y logarítmico</span>}
        </div>
      )}
      <div className="history-body">
        {history.length === 0 ? (
          <p className="history-empty">Ejecuta sentencias para comparar su I/O aquí.</p>
        ) : historyView === 'chart' ? (
          <IoHistoryChart items={history} logScale={logScale} />
        ) : (
          <div className="history-table-wrap">
            <HistoryTable items={history} />
          </div>
        )}
      </div>
    </div>
  )
}
