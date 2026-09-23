import { Fragment, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import {
  fetchPage,
  fetchTableFiles,
  isAbortError,
  toApiError,
  type ApiError,
  type JsonValue,
  type PageDump,
  type PageHeader,
  type TableFile,
  type TableInfo,
} from '../api'
import { fmtInt } from '../lib/format'
import { Badge, StructureBadge } from './Badges'
import { Icon } from './Icon'

/** Tamaño de la cabecera común de página (formato '<IBBHHiiixx'). */
const PAGE_HEADER_BYTES = 24
const MAX_CHIPS = 256
const MAX_TABLE_ROWS = 500

interface PageInspectorProps {
  table: string
  tableInfo: TableInfo | undefined
  onClose(): void
}

export function PageInspector({ table, tableInfo, onClose }: PageInspectorProps) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [files, setFiles] = useState<TableFile[] | null>(null)
  const [filesError, setFilesError] = useState<ApiError | null>(null)
  const [fileKey, setFileKey] = useState<string | null>(null)
  const [pageId, setPageId] = useState(0)
  const [pageDraft, setPageDraft] = useState<string | null>(null)
  const [dump, setDump] = useState<PageDump | null>(null)
  const [dumpError, setDumpError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailsView, setDetailsView] = useState<'pretty' | 'json'>('pretty')

  // Solo se cierra al hacer clic en el fondo si la pulsación también empezó en el fondo
  const pressedBackdrop = useRef(false)

  useEffect(() => {
    const dialog = dialogRef.current
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    if (dialog && !dialog.open) dialog.showModal()
    return () => {
      dialog?.close()
      // Devolver el foco al botón que abrió el inspector
      if (opener?.isConnected) opener.focus({ preventScroll: true })
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchTableFiles(table, controller.signal)
      .then((list) => {
        setFiles(list)
        const initial = list.find((f) => f.key === 'data') ?? list[0]
        if (initial) {
          setFileKey(initial.key)
          setPageId(initial.num_pages > 1 ? 1 : 0)
        }
      })
      .catch((err: unknown) => {
        if (!isAbortError(err)) setFilesError(toApiError(err))
      })
    return () => controller.abort()
  }, [table])

  const file = files?.find((f) => f.key === fileKey) ?? null
  const maxPage = file ? Math.max(0, file.num_pages - 1) : 0

  useEffect(() => {
    if (fileKey === null) return
    const controller = new AbortController()
    setLoading(true)
    // Pequeño debounce: al mantener pulsado +/− solo se pide la última página
    const timer = window.setTimeout(() => {
      fetchPage(table, pageId, fileKey, controller.signal)
        .then((result) => {
          setDump(result)
          setDumpError(null)
        })
        .catch((err: unknown) => {
          if (!isAbortError(err)) setDumpError(toApiError(err))
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false)
        })
    }, 120)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [table, fileKey, pageId])

  const goTo = (n: number) => setPageId(Math.min(Math.max(Math.trunc(n), 0), maxPage))

  const openFile = (key: string, page?: number) => {
    const target = files?.find((f) => f.key === key)
    if (!target) return
    setFileKey(key)
    setPageId(page ?? (target.num_pages > 1 ? 1 : 0))
  }

  const commitDraft = () => {
    const n = Number.parseInt(pageDraft ?? '', 10)
    setPageDraft(null)
    if (Number.isFinite(n)) goTo(n)
  }

  // Esc cierra (no se depende solo del evento cancel nativo); ← / → cambian de página
  const onKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
      return
    }
    const target = event.target as HTMLElement
    if (target.closest('input, select, textarea')) return
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      goTo(pageId - 1)
    } else if (event.key === 'ArrowRight') {
      event.preventDefault()
      goTo(pageId + 1)
    }
  }

  const columnNames = tableInfo?.columns.map((c) => c.name) ?? []
  const isDataFile = fileKey === 'data' || fileKey === 'overflow'

  return (
    <dialog
      ref={dialogRef}
      className="modal"
      aria-labelledby="inspector-title"
      onCancel={(e) => {
        e.preventDefault()
        onClose()
      }}
      onClose={() => {
        // Si el navegador cerró el diálogo por su cuenta, se sincroniza el estado de React
        if (!dialogRef.current?.open) onClose()
      }}
      onMouseDown={(e) => {
        pressedBackdrop.current = e.target === e.currentTarget
      }}
      onClick={(e) => {
        if (pressedBackdrop.current && e.target === e.currentTarget) onClose()
        pressedBackdrop.current = false
      }}
      onKeyDown={onKeyDown}
    >
      <div className="modal-inner">
        <header className="modal-header">
          <h2 id="inspector-title" className="modal-title">
            <Icon name="binary" size={18} />
            Inspector de páginas
            <span className="modal-subtitle">
              <code>{table}</code>
              {tableInfo && <StructureBadge structure={tableInfo.organization} />}
            </span>
          </h2>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Cerrar inspector" title="Cerrar (Esc)">
            <Icon name="x" />
          </button>
        </header>

        {filesError ? (
          <div className="modal-message">
            <InlineError error={filesError} />
          </div>
        ) : files === null ? (
          <div className="modal-message">
            <span className="spinner spinner-lg" aria-hidden="true" />
            Cargando archivos de la tabla…
          </div>
        ) : files.length === 0 ? (
          <div className="modal-message">La tabla no tiene archivos inspeccionables.</div>
        ) : (
          <>
            <div className="inspector-toolbar">
              <div className="file-tabs" role="group" aria-label="Archivo">
                {files.map((f) => (
                  <button
                    key={f.key}
                    type="button"
                    className="file-tab"
                    aria-pressed={f.key === fileKey}
                    onClick={() => openFile(f.key)}
                    title={f.path}
                  >
                    <span className="file-tab-key">{f.key}</span>
                    <StructureBadge structure={f.kind} />
                    <span className="file-tab-pages">{fmtInt(f.num_pages)} págs.</span>
                  </button>
                ))}
              </div>
              {file && (
                <div className="page-nav">
                  <button type="button" className="btn btn-ghost btn-icon" onClick={() => goTo(0)} disabled={pageId <= 0} aria-label="Primera página" title="Página 0">
                    <Icon name="chevronsLeft" />
                  </button>
                  <button type="button" className="btn btn-secondary btn-icon" onClick={() => goTo(pageId - 1)} disabled={pageId <= 0} aria-label="Página anterior" title="Anterior (←)">
                    <span aria-hidden="true" className="step-sign">−</span>
                  </button>
                  <label className="page-input-label">
                    <span className="sr-only">Número de página</span>
                    <input
                      className="input page-input"
                      type="number"
                      min={0}
                      max={maxPage}
                      value={pageDraft ?? String(pageId)}
                      onChange={(e) => setPageDraft(e.target.value)}
                      onBlur={commitDraft}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') commitDraft()
                      }}
                    />
                  </label>
                  <button type="button" className="btn btn-secondary btn-icon" onClick={() => goTo(pageId + 1)} disabled={pageId >= maxPage} aria-label="Página siguiente" title="Siguiente (→)">
                    <span aria-hidden="true" className="step-sign">+</span>
                  </button>
                  <button type="button" className="btn btn-ghost btn-icon" onClick={() => goTo(maxPage)} disabled={pageId >= maxPage} aria-label="Última página" title={`Página ${maxPage}`}>
                    <Icon name="chevronsRight" />
                  </button>
                  <span className="page-range">
                    rango 0–{fmtInt(maxPage)} · {fmtInt(file.page_size)} B/página
                  </span>
                  {loading && <span className="spinner" aria-label="Cargando página" />}
                </div>
              )}
            </div>

            <div className={`inspector-body${loading && dump ? ' is-busy' : ''}`}>
              {dumpError && !dump ? (
                <div className="modal-message">
                  <InlineError error={dumpError} />
                </div>
              ) : !dump ? (
                <div className="modal-message">
                  <span className="spinner spinner-lg" aria-hidden="true" />
                  Leyendo página…
                </div>
              ) : (
                <>
                  <div className="inspector-col">
                    {dumpError && <InlineError error={dumpError} />}
                    <div className="inspector-page-title">
                      <span>
                        Página <strong>{fmtInt(dump.page_id)}</strong> de <code>{dump.path}</code>
                      </span>
                      <Badge tone="neutral" size="sm">
                        {dump.header.page_type_name ?? `tipo ${dump.header.page_type}`}
                      </Badge>
                    </div>
                    <h3 className="section-title">Cabecera ({PAGE_HEADER_BYTES} B)</h3>
                    <HeaderTable
                      header={dump.header}
                      maxPage={dump.num_pages - 1}
                      onGo={goTo}
                      onOverflow={
                        dump.header.page_type_name === 'SEQ_MAIN' && files.some((f) => f.key === 'overflow')
                          ? (page) => openFile('overflow', page)
                          : undefined
                      }
                    />
                    <div className="section-head">
                      <h3 className="section-title">Contenido decodificado</h3>
                      <div className="segmented" role="group" aria-label="Formato del contenido">
                        <button type="button" aria-pressed={detailsView === 'pretty'} onClick={() => setDetailsView('pretty')}>
                          Vista
                        </button>
                        <button type="button" aria-pressed={detailsView === 'json'} onClick={() => setDetailsView('json')}>
                          JSON
                        </button>
                      </div>
                    </div>
                    {detailsView === 'json' ? (
                      <pre className="json-raw">{JSON.stringify(dump.details, null, 2)}</pre>
                    ) : (
                      <DetailsView details={dump.details} columns={isDataFile ? columnNames : []} onGo={goTo} />
                    )}
                  </div>
                  <div className="inspector-col inspector-hex">
                    <HexdumpView hexdump={dump.hexdump} />
                  </div>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </dialog>
  )
}

function InlineError({ error }: { error: ApiError }) {
  return (
    <div className="inline-alert" role="alert">
      <Icon name="alert" size={14} />
      <span>
        <strong>{error.errorType}</strong> · {error.message}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Cabecera

const HEADER_HINTS: Record<string, string> = {
  page_id: 'u32 · id del bloque dentro del archivo',
  page_type: 'u8 · tipo de página',
  flags: 'u8 · específico del tipo (p. ej. local depth)',
  record_count: 'u16 · registros o claves activos',
  free_space_offset: 'u16 · primer slot libre (0xFFFF = llena)',
  next_page_id: 'i32 · encadenamiento siguiente',
  prev_page_id: 'i32 · encadenamiento anterior',
  aux_page_id: 'i32 · específico del tipo (p. ej. overflow)',
}

interface HeaderTableProps {
  header: PageHeader
  maxPage: number
  onGo(page: number): void
  onOverflow?: (page: number) => void
}

function HeaderTable({ header, maxPage, onGo, onOverflow }: HeaderTableProps) {
  const renderValue = (key: string, value: JsonValue): ReactNode => {
    if (typeof value !== 'number') return <span className="mono">{cellText(value)}</span>
    if (key === 'page_type' && typeof header.page_type_name === 'string') {
      return (
        <span className="mono">
          {value} <span className="muted">· {header.page_type_name}</span>
        </span>
      )
    }
    if (key === 'free_space_offset' && value === 0xffff) {
      return (
        <span className="mono">
          65535 <span className="muted">· 0xFFFF, página llena</span>
        </span>
      )
    }
    if (key.endsWith('_page_id') && key !== 'page_id') {
      if (value < 0) return <span className="mono muted">{value} · nulo</span>
      const sameFile = key !== 'aux_page_id'
      if (sameFile && value <= maxPage) {
        return (
          <button type="button" className="link-button mono" onClick={() => onGo(value)}>
            {value} <Icon name="arrowRight" size={12} />
          </button>
        )
      }
      if (!sameFile && onOverflow) {
        return (
          <button type="button" className="link-button mono" onClick={() => onOverflow(value)} title="Abrir en el archivo de overflow">
            {value} <Icon name="arrowRight" size={12} /> overflow
          </button>
        )
      }
    }
    return <span className="mono">{value}</span>
  }

  return (
    <table className="kv-table">
      <tbody>
        {Object.entries(header)
          .filter(([key]) => key !== 'page_type_name')
          .map(([key, value]) => (
            <tr key={key}>
              <th scope="row">
                <span className="mono">{key}</span>
                {HEADER_HINTS[key] && <span className="kv-hint">{HEADER_HINTS[key]}</span>}
              </th>
              <td>{renderValue(key, value)}</td>
            </tr>
          ))}
      </tbody>
    </table>
  )
}

// ---------------------------------------------------------------------------
// Detalles (forma libre según el tipo de página)

type Scalar = string | number | boolean | null
type JsonObject = { [key: string]: JsonValue }

const isScalar = (v: JsonValue): v is Scalar => v === null || typeof v !== 'object'
const isObject = (v: JsonValue): v is JsonObject => typeof v === 'object' && v !== null && !Array.isArray(v)
/** Claves cuyos valores son ids de página del mismo archivo (nodos B+, directorio hash). */
const NAVIGABLE_KEYS = /^(children|child_pages|child_page_ids|pointers|buckets|bucket_pages|directory)$/i

function formatScalar(v: Scalar): string {
  if (v === null) return 'null'
  if (typeof v === 'number' && !Number.isInteger(v)) return v.toFixed(2)
  return String(v)
}

interface DetailsViewProps {
  details: JsonValue
  /** Nombres de columnas de la tabla, para rotular los valores de los registros. */
  columns: string[]
  onGo(page: number): void
}

function DetailsView({ details, columns, onGo }: DetailsViewProps) {
  if (!isObject(details)) return <JsonBlock name="" value={details} columns={columns} onGo={onGo} />
  const entries = Object.entries(details)
  const scalars = entries.filter(([, v]) => isScalar(v))
  const complex = entries.filter(([, v]) => !isScalar(v))
  return (
    <div className="details">
      {scalars.length > 0 && (
        <table className="kv-table">
          <tbody>
            {scalars.map(([key, value]) => (
              <tr key={key}>
                <th scope="row">
                  <span className="mono">{key}</span>
                </th>
                <td className="mono">{formatScalar(value as Scalar)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {complex.map(([key, value]) => (
        <section key={key} className="details-section">
          <h4 className="details-title">
            <span className="mono">{key}</span>
            {Array.isArray(value) && <span className="count">{fmtInt(value.length)}</span>}
          </h4>
          <JsonBlock name={key} value={value} columns={columns} onGo={onGo} />
        </section>
      ))}
      {entries.length === 0 && <p className="muted-note">Sin detalles para esta página.</p>}
    </div>
  )
}

interface JsonBlockProps {
  name: string
  value: JsonValue
  columns: string[]
  onGo(page: number): void
}

function JsonBlock({ name, value, columns, onGo }: JsonBlockProps): ReactNode {
  if (isScalar(value)) return <span className="mono">{formatScalar(value)}</span>
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">[ ] vacío</span>
    if (value.every(isScalar)) return <ChipList name={name} items={value} onGo={onGo} />
    if (value.every(isObject)) return <ObjectTable rows={value} columns={columns} />
    return <pre className="json-raw">{JSON.stringify(value, null, 2)}</pre>
  }
  return (
    <dl className="json-object">
      {Object.entries(value).map(([key, v]) => (
        <Fragment key={key}>
          <dt className="mono">{key}</dt>
          <dd>
            <JsonBlock name={key} value={v} columns={columns} onGo={onGo} />
          </dd>
        </Fragment>
      ))}
    </dl>
  )
}

function ChipList({ name, items, onGo }: { name: string; items: Scalar[]; onGo(page: number): void }) {
  const navigable = NAVIGABLE_KEYS.test(name)
  const shown = items.slice(0, MAX_CHIPS)
  return (
    <div className="chip-list">
      {shown.map((item, i) =>
        navigable && typeof item === 'number' && Number.isInteger(item) && item >= 0 ? (
          <button key={i} type="button" className="chip chip-link" onClick={() => onGo(item)} title={`Ir a la página ${item}`}>
            {item}
          </button>
        ) : (
          <span key={i} className="chip">
            {formatScalar(item)}
          </span>
        ),
      )}
      {items.length > shown.length && <span className="chip chip-more">+{fmtInt(items.length - shown.length)} más</span>}
    </div>
  )
}

function cellText(v: JsonValue | undefined): string {
  if (v === undefined) return ''
  if (isScalar(v)) return formatScalar(v)
  if (Array.isArray(v) && v.every(isScalar)) return `[${v.map(formatScalar).join(', ')}]`
  return JSON.stringify(v)
}

function ObjectTable({ rows, columns }: { rows: JsonObject[]; columns: string[] }) {
  const keys = useMemo(() => {
    const seen = new Set<string>()
    rows.forEach((row) => Object.keys(row).forEach((k) => seen.add(k)))
    return [...seen]
  }, [rows])

  // Si cada fila trae "values" con tantas posiciones como columnas tiene la tabla, se rotulan
  const expandValues =
    columns.length > 0 &&
    keys.includes('values') &&
    rows.every((row) => Array.isArray(row.values) && row.values.length === columns.length)
  const plainKeys = expandValues ? keys.filter((k) => k !== 'values') : keys
  const shown = rows.slice(0, MAX_TABLE_ROWS)

  return (
    <div className="object-table-wrap">
      <table className="mini-table object-table">
        <thead>
          <tr>
            {plainKeys.map((k) => (
              <th key={k} scope="col">
                {k}
              </th>
            ))}
            {expandValues &&
              columns.map((c) => (
                <th key={`col-${c}`} scope="col" className="col-from-schema">
                  {c}
                </th>
              ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, i) => (
            <tr key={i}>
              {plainKeys.map((k) => {
                const v = row[k]
                return (
                  <td key={k} className={typeof v === 'number' ? 'num' : undefined}>
                    {cellText(v)}
                  </td>
                )
              })}
              {expandValues &&
                (row.values as JsonValue[]).map((v, j) => (
                  <td key={`v-${j}`} className={typeof v === 'number' ? 'num' : undefined}>
                    {cellText(v)}
                  </td>
                ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && (
        <p className="muted-note">
          Mostrando {fmtInt(shown.length)} de {fmtInt(rows.length)} filas (usa la vista JSON para verlas todas).
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Hexdump

const HEX_LINE = /^([0-9a-fA-F]{4,16})(\s+)(.*?)(\s+\|.*\|)?\s*$/
const HEX_BYTE = /^[0-9a-fA-F]{2}$/

/** origin: offset de la primera línea (por si el backend imprime offsets de archivo y no de página). */
function HexLine({ line, origin }: { line: string; origin: number }) {
  const match = HEX_LINE.exec(line)
  if (!match) return <span className="hx-raw">{line}</span>
  const [, offsetText = '', spacing = '', hexPart = '', asciiPart = ''] = match
  const base = Number.parseInt(offsetText, 16) - origin
  let index = 0
  const tokens = hexPart.split(/(\s+)/).map((token, i) => {
    if (!HEX_BYTE.test(token)) return token
    const relative = base + index
    index += 1
    const classes = ['hx-byte']
    if (relative < PAGE_HEADER_BYTES) classes.push('hx-header')
    else if (token === '00') classes.push('hx-zero')
    return (
      <span key={i} className={classes.join(' ')}>
        {token}
      </span>
    )
  })
  return (
    <>
      <span className="hx-offset">{offsetText}</span>
      {spacing}
      {tokens}
      {asciiPart && <span className="hx-ascii">{asciiPart}</span>}
    </>
  )
}

function HexdumpView({ hexdump }: { hexdump: string }) {
  const [copied, setCopied] = useState(false)
  const lines = useMemo(() => hexdump.replace(/\n+$/, '').split('\n'), [hexdump])
  const origin = useMemo(() => {
    const first = lines.map((l) => HEX_LINE.exec(l)).find((m) => m !== null)
    return first?.[1] ? Number.parseInt(first[1], 16) : 0
  }, [lines])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(hexdump)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  return (
    <>
      <div className="section-head">
        <h3 className="section-title">Hexdump</h3>
        <div className="hex-legend">
          <span>
            <span className="hex-swatch hx-header" /> cabecera
          </span>
          <span>
            <span className="hex-swatch hx-zero" /> bytes 00
          </span>
          <button type="button" className="btn btn-ghost btn-xs" onClick={copy}>
            <Icon name={copied ? 'check' : 'copy'} size={13} />
            {copied ? 'Copiado' : 'Copiar'}
          </button>
        </div>
      </div>
      <pre className="hexdump" tabIndex={0} aria-label="Hexdump de la página">
        {lines.map((line, i) => (
          <Fragment key={i}>
            <HexLine line={line} origin={origin} />
            {'\n'}
          </Fragment>
        ))}
      </pre>
    </>
  )
}
