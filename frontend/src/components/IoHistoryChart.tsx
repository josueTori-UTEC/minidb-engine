import { useMemo } from 'react'
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type BarShapeProps,
  type DotItemDotProps,
  type TooltipContentProps,
} from 'recharts'
import { fmtCompact, fmtInt, fmtMs } from '../lib/format'
import type { HistoryEntry } from '../types'

interface ChartDatum {
  label: string
  entry: HistoryEntry
  readsY: number
  writesY: number
  estY: number | null
}

// Escala log "simétrica": 0 → 0, 1 → 1, 10 → 2, 100 → 3… así el 0 y el 1 siguen siendo distinguibles.
const toLog = (v: number) => (v <= 0 ? 0 : Math.log10(v) + 1)

function buildData(items: readonly HistoryEntry[], log: boolean): ChartDatum[] {
  return items.map((entry) => {
    const label = `#${entry.id} ${entry.kind}`
    if (!log) {
      return { label, entry, readsY: entry.reads, writesY: entry.writes, estY: entry.estimated }
    }
    // Las barras se apilan en espacio log: la base es log(lecturas) y el tope log(lecturas + escrituras)
    const readsTop = toLog(entry.reads)
    return {
      label,
      entry,
      readsY: readsTop,
      writesY: toLog(entry.reads + entry.writes) - readsTop,
      estY: entry.estimated === null ? null : toLog(entry.estimated),
    }
  })
}

/** Rectángulo con extremo de dato redondeado (4px) y base recta. */
function segmentPath(x: number, y: number, w: number, h: number, roundTop: boolean): string {
  const r = roundTop ? Math.min(4, w / 2, h) : 0
  if (r <= 0) return `M${x},${y}h${w}v${h}h${-w}Z`
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`
}

function ReadsShape({ x, y, width, height, fill, payload }: BarShapeProps) {
  if (!(height > 0) || !(width > 0)) return null
  const datum = payload as ChartDatum
  return <path d={segmentPath(x, y, width, height, datum.entry.writes <= 0)} fill={fill} />
}

function WritesShape({ x, y, width, height, fill, payload }: BarShapeProps) {
  const datum = payload as ChartDatum
  // 2px de separación con el segmento de lecturas, tomados del segmento superior
  const gap = datum.entry.reads > 0 ? 2 : 0
  const h = height - gap
  if (!(h > 0) || !(width > 0)) return null
  return <path d={segmentPath(x, y, width, h, true)} fill={fill} />
}

function EstimateDot({ cx, cy, payload }: DotItemDotProps) {
  const datum = payload as ChartDatum | undefined
  if (cx == null || cy == null || !datum || datum.estY === null) return null
  return <circle cx={cx} cy={cy} r={4.5} fill="var(--series-est)" stroke="var(--panel)" strokeWidth={2} />
}

function truncate(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

function HistoryTooltip({ active, payload }: TooltipContentProps) {
  const datum = active ? (payload?.[0]?.payload as ChartDatum | undefined) : undefined
  if (!datum) return null
  const e = datum.entry
  return (
    <div className="chart-tooltip">
      <div className="tt-title">
        #{e.id} {e.kind}
        {e.page !== undefined && <span className="tt-page"> · página {e.page}</span>}
      </div>
      <code className="tt-sql">{truncate(e.sql, 96)}</code>
      <div className="tt-rows">
        <span className="tt-key key-reads" />
        <strong>{fmtInt(e.reads)}</strong>
        <span>lecturas</span>
        <span className="tt-key key-writes" />
        <strong>{fmtInt(e.writes)}</strong>
        <span>escrituras</span>
        <span className="tt-key key-est" />
        <strong>{e.estimated === null ? '—' : fmtInt(e.estimated)}</strong>
        <span>I/O estimado</span>
      </div>
      <div className="tt-foot">
        {fmtInt(e.reads + e.writes)} I/O reales · {fmtMs(e.totalMs)} ms
      </div>
    </div>
  )
}

interface IoHistoryChartProps {
  items: readonly HistoryEntry[]
  logScale: boolean
}

export function IoHistoryChart({ items, logScale }: IoHistoryChartProps) {
  const data = useMemo(() => buildData(items, logScale), [items, logScale])

  const yAxis = useMemo(() => {
    if (!logScale) return { domain: [0, 'auto'] as [number, 'auto'], ticks: undefined, format: fmtCompact }
    const max = Math.max(1, ...data.map((d) => Math.max(d.readsY + d.writesY, d.estY ?? 0)))
    const top = Math.ceil(max)
    const ticks = Array.from({ length: top + 1 }, (_, k) => k)
    const format = (k: number) => (k === 0 ? '0' : fmtCompact(10 ** (k - 1)))
    return { domain: [0, top] as [number, number], ticks, format }
  }, [data, logScale])

  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 10, right: 8, bottom: 0, left: 22 }} barCategoryGap="22%">
        <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
        <XAxis
          dataKey="label"
          interval="preserveEnd"
          angle={-45}
          textAnchor="end"
          height={82}
          tick={{ fontSize: 10, fill: 'var(--text-3)' }}
          tickLine={false}
          axisLine={{ stroke: 'var(--chart-axis)' }}
        />
        <YAxis
          width={34}
          domain={yAxis.domain}
          ticks={yAxis.ticks}
          allowDecimals={false}
          tickFormatter={yAxis.format}
          tick={{ fontSize: 10, fill: 'var(--text-3)' }}
          tickLine={false}
          axisLine={false}
        />
        <Tooltip content={HistoryTooltip} cursor={{ fill: 'var(--chart-cursor)' }} isAnimationActive={false} />
        <Bar
          dataKey="readsY"
          name="Lecturas"
          stackId="io"
          fill="var(--series-reads)"
          maxBarSize={24}
          shape={ReadsShape}
          isAnimationActive={false}
        />
        <Bar
          dataKey="writesY"
          name="Escrituras"
          stackId="io"
          fill="var(--series-writes)"
          maxBarSize={24}
          shape={WritesShape}
          isAnimationActive={false}
        />
        <Line
          dataKey="estY"
          name="I/O estimado"
          stroke="none"
          dot={EstimateDot}
          activeDot={false}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

/** Vista de tabla equivalente al gráfico (accesible y con valores exactos). */
export function HistoryTable({ items }: { items: readonly HistoryEntry[] }) {
  return (
    <table className="mini-table history-table">
      <thead>
        <tr>
          <th scope="col">#</th>
          <th scope="col">Tipo</th>
          <th scope="col" className="num">Lect.</th>
          <th scope="col" className="num">Escr.</th>
          <th scope="col" className="num">Estim.</th>
          <th scope="col" className="num">ms</th>
        </tr>
      </thead>
      <tbody>
        {[...items].reverse().map((e) => (
          <tr key={e.id} title={e.sql}>
            <td className="muted">{e.id}</td>
            <td>{e.kind}</td>
            <td className="num">{fmtInt(e.reads)}</td>
            <td className="num">{fmtInt(e.writes)}</td>
            <td className="num">{e.estimated === null ? '—' : fmtInt(e.estimated)}</td>
            <td className="num">{fmtMs(e.totalMs)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
