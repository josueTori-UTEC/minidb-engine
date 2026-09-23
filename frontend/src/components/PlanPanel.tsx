import type { Metrics, Plan, PlanCandidate, QueryResponse, ReorganizeResponse, ReorganizeSnapshot } from '../api'
import { fmtInt, fmtMs } from '../lib/format'
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
      {plan && <EstimateVsActual estimated={plan.estimated_io} metrics={metrics} />}
      {plan && plan.candidates.length > 0 && <Candidates candidates={plan.candidates} />}
    </>
  )
}

function PlanDetails({ plan }: { plan: Plan }) {
  return (
    <>
      <div className="plan-hero">
        <PlanBadge type={plan.type} size="lg" />
        <p className="plan-desc">{PLAN_DESCRIPTION[plan.type] ?? 'Plan reportado por el backend.'}</p>
      </div>
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
