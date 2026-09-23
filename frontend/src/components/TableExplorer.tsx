import { useState } from 'react'
import type { TableInfo } from '../api'
import { columnTypeLabel, fmtInt } from '../lib/format'
import { StructureBadge } from './Badges'
import { Icon } from './Icon'

export type CatalogStatus = 'loading' | 'ready' | 'error'

export interface PlanHighlight {
  table: string
  index: string | null
}

interface TableExplorerProps {
  tables: readonly TableInfo[]
  status: CatalogStatus
  error: string | null
  locked: boolean
  reorganizing: string | null
  highlight: PlanHighlight | null
  onRefresh(): void
  onPickTable(name: string): void
  onReorganize(name: string): void
  onInspect(name: string): void
}

export function TableExplorer(props: TableExplorerProps) {
  const { tables, status, error, onRefresh } = props
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set())

  const toggle = (name: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  return (
    <section className="panel panel-explorer" aria-labelledby="explorer-title">
      <header className="panel-header">
        <h2 className="panel-title" id="explorer-title">
          <Icon name="database" />
          Explorador de tablas
        </h2>
        {tables.length > 0 && <span className="panel-count">{tables.length}</span>}
        <div className="panel-actions">
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={onRefresh}
            title="Actualizar catálogo"
            aria-label="Actualizar catálogo"
          >
            <Icon name="refresh" className={status === 'loading' ? 'spin' : undefined} />
          </button>
        </div>
      </header>

      <div className="panel-body explorer-body">
        {status === 'error' && tables.length > 0 && (
          <div className="inline-alert" role="alert">
            <Icon name="alert" size={14} />
            <span>No se pudo actualizar el catálogo.</span>
            <button type="button" className="link-button" onClick={onRefresh}>
              Reintentar
            </button>
          </div>
        )}

        {tables.length === 0 && status === 'loading' && (
          <div className="empty-state">
            <span className="spinner spinner-lg" aria-hidden="true" />
            <p className="empty-title">Cargando catálogo…</p>
          </div>
        )}
        {tables.length === 0 && status === 'error' && (
          <div className="empty-state">
            <Icon name="alert" size={28} className="empty-icon is-danger" />
            <p className="empty-title">No se pudo cargar el catálogo</p>
            <p className="empty-text">{error}</p>
            <button type="button" className="btn btn-secondary btn-sm" onClick={onRefresh}>
              <Icon name="refresh" size={14} />
              Reintentar
            </button>
          </div>
        )}
        {tables.length === 0 && status === 'ready' && (
          <div className="empty-state">
            <Icon name="table" size={28} className="empty-icon" />
            <p className="empty-title">Aún no hay tablas</p>
            <p className="empty-text">
              Crea una con <code>CREATE TABLE … USING HEAP</code> o <code>USING SEQUENTIAL</code>. El menú
              Ejemplos del editor tiene sentencias listas para usar.
            </p>
          </div>
        )}

        {tables.map((table) => (
          <TableCard
            key={table.name}
            table={table}
            expanded={!collapsed.has(table.name)}
            onToggle={() => toggle(table.name)}
            {...props}
          />
        ))}
      </div>
    </section>
  )
}

interface TableCardProps extends TableExplorerProps {
  table: TableInfo
  expanded: boolean
  onToggle(): void
}

function TableCard({ table, expanded, onToggle, locked, reorganizing, highlight, onPickTable, onReorganize, onInspect }: TableCardProps) {
  const isSequential = table.organization === 'SEQUENTIAL'
  const isActive = highlight?.table === table.name
  const bodyId = `table-${table.name}-body`

  return (
    <article className={`table-card${isActive ? ' is-active' : ''}`} aria-label={`Tabla ${table.name}`}>
      <div className="table-card-head">
        <button
          type="button"
          className="tree-toggle"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-controls={bodyId}
          aria-label={expanded ? `Contraer ${table.name}` : `Expandir ${table.name}`}
        >
          <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={14} />
        </button>
        <button
          type="button"
          className="table-name"
          onClick={() => onPickTable(table.name)}
          title={`${table.name} · clic para escribir SELECT * FROM ${table.name}; en el editor`}
        >
          <Icon name="table" size={14} />
          <span>{table.name}</span>
        </button>
        <StructureBadge structure={table.organization} />
      </div>

      {expanded && (
        <div className="table-card-body" id={bodyId}>
          <dl className="stat-grid">
            <Stat label="Registros" value={fmtInt(table.record_count)} />
            <Stat label="Páginas" value={fmtInt(table.page_count)} title="Páginas de datos (sin la página 0 de metadatos)" />
            {isSequential && (
              <Stat
                label="Overflow"
                value={fmtInt(table.overflow_page_count ?? 0)}
                title="Páginas en el área de overflow"
                tone={(table.overflow_page_count ?? 0) > 0 ? 'warn' : undefined}
              />
            )}
            <Stat label="Página" value={`${fmtInt(table.page_size)} B`} title="Tamaño de página del archivo" />
            <Stat label="Registro" value={`${fmtInt(table.record_size)} B`} title="Tamaño fijo de cada registro" />
            <Stat label="Reg./pág." value={fmtInt(table.records_per_page)} title="Registros por página de datos" />
          </dl>

          <h3 className="section-label">
            Columnas <span className="count">{table.columns.length}</span>
          </h3>
          <ul className="column-list">
            {table.columns.map((col) => (
              <li key={col.name}>
                <span className="col-key" title={col.primary_key ? 'Clave primaria' : undefined}>
                  {col.primary_key && <Icon name="key" size={12} />}
                </span>
                <span className="col-name">{col.name}</span>
                <span className="col-type">{columnTypeLabel(col)}</span>
                {col.primary_key && <span className="pk-tag">PK</span>}
              </li>
            ))}
          </ul>

          <h3 className="section-label">
            Índices <span className="count">{table.indexes.length}</span>
          </h3>
          {table.indexes.length === 0 ? (
            <p className="muted-note">Sin índices activos.</p>
          ) : (
            <ul className="index-list">
              {table.indexes.map((index) => {
                const inUse = isActive && highlight?.index === index.name
                return (
                  <li key={index.name} className={inUse ? 'is-used' : undefined}>
                    <div className="index-head">
                      <span className="index-name">{index.name}</span>
                      <StructureBadge structure={index.type} />
                      {inUse && (
                        <span className="used-tag" title="Índice elegido por el último plan de ejecución">
                          en uso
                        </span>
                      )}
                    </div>
                    <div className="index-meta">
                      <span>
                        col. <code>{index.column}</code>
                      </span>
                      {index.type === 'HASH' ? (
                        <span title="Profundidad global del directorio">prof. global {index.global_depth ?? '—'}</span>
                      ) : (
                        <span title="Altura del árbol B+">altura {index.height ?? '—'}</span>
                      )}
                      <span>{fmtInt(index.page_count)} págs.</span>
                      <span>{fmtInt(index.entry_count)} entradas</span>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}

          <div className="table-card-actions">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={(e) => {
                // Safari no enfoca botones al hacer clic: así el foco vuelve aquí al cerrar el inspector
                e.currentTarget.focus()
                onInspect(table.name)
              }}
            >
              <Icon name="binary" size={14} />
              Inspeccionar páginas
            </button>
            {isSequential && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => onReorganize(table.name)}
                disabled={locked}
                title="Fusionar el overflow con el área principal (POST /api/tables/reorganize)"
              >
                {reorganizing === table.name ? <span className="spinner" aria-hidden="true" /> : <Icon name="layers" size={14} />}
                {reorganizing === table.name ? 'Reorganizando…' : 'Reorganizar'}
              </button>
            )}
          </div>
        </div>
      )}
    </article>
  )
}

function Stat({ label, value, title, tone }: { label: string; value: string; title?: string; tone?: 'warn' }) {
  return (
    <div className={`stat${tone ? ` is-${tone}` : ''}`} title={title}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}
