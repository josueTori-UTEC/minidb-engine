import L from 'leaflet'
import { useEffect, useMemo, useRef, useState } from 'react'
import { isMediaFile, mediaUrl, type CellValue, type QueryResponse, type SpatialPlan } from '../api'
import { fmtInt, fmtNumber, formatCell, formatPoint, isPoint, plural } from '../lib/format'
import {
  boxToBounds,
  buildPopup,
  circleBounds,
  fitMap,
  groupByPoint,
  markerRadius,
  refitWhenShown,
  toLatLng,
  useLeafletMap,
  type PopupItem,
} from '../lib/map'

interface ResultsMapProps {
  response: QueryResponse
  /** Índices de las columnas cuyos valores son puntos [lon, lat]. */
  pointColumns: readonly number[]
}

interface MapRow {
  /** Número de fila dentro del resultado completo (1-based). */
  number: number
  cells: CellValue[]
}

/** Vista de mapa del resultado: un marcador por ubicación y, si la consulta fue espacial, su geometría. */
export function ResultsMap({ response, pointColumns }: ResultsMapProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const map = useLeafletMap(containerRef)
  const [showMbrs, setShowMbrs] = useState(false)
  const [columnChoice, setColumnChoice] = useState<number | null>(null)

  const pointColumn = columnChoice !== null && pointColumns.includes(columnChoice) ? columnChoice : (pointColumns[0] ?? -1)
  const spatial = response.plan?.spatial ?? null
  const mbrs = spatial?.mbrs ?? []

  const groups = useMemo(() => {
    const offset = (response.page - 1) * response.page_size
    const rows: MapRow[] = response.rows.map((cells, i) => ({ number: offset + i + 1, cells }))
    return groupByPoint(rows, (row) => {
      const value = row.cells[pointColumn]
      return isPoint(value) ? value : null
    })
  }, [response, pointColumn])
  const plotted = groups.reduce((total, group) => total + group.items.length, 0)

  useEffect(() => {
    if (!map) return
    const layer = L.featureGroup().addTo(map)
    const bounds = L.latLngBounds([])

    if (showMbrs) {
      // Primero los nodos internos y encima las hojas, para que los rectángulos chicos queden a la vista
      const ordered = [...(spatial?.mbrs ?? [])].sort((a, b) => Number(a.leaf) - Number(b.leaf) || b.level - a.level)
      for (const node of ordered) {
        if (!Array.isArray(node.mbr) || node.mbr.length !== 4) continue
        const rect = boxToBounds(node.mbr)
        L.rectangle(rect, {
          className: node.leaf ? 'map-mbr map-mbr-leaf' : 'map-mbr map-mbr-internal',
          color: node.leaf ? '#15803d' : '#7e22ce',
          weight: node.leaf ? 1.5 : 2,
          dashArray: node.leaf ? undefined : '6 4',
          fill: false,
        })
          .bindTooltip(`${node.leaf ? 'Hoja' : 'Nodo interno'} · página ${node.page_id} · nivel ${node.level}`, { sticky: true })
          .addTo(layer)
        bounds.extend(rect)
      }
    }

    if (spatial) drawQuery(spatial, layer, bounds)

    const columns = response.columns
    for (const group of groups) {
      const count = group.items.length
      const items: PopupItem[] = group.items.map((row) => {
        const image = row.cells.find((cell, j) => columns[j] === 'imagen' && isMediaFile(cell))
        return {
          title: `Fila ${fmtInt(row.number)}`,
          image: typeof image === 'string' ? mediaUrl(image) : undefined,
          fields: columns.flatMap((name, j) => (j === pointColumn ? [] : [[name, formatCell(row.cells[j]).text] as const])),
        }
      })
      const heading = `${formatPoint(group.point)}${count > 1 ? ` · ${plural(count, 'fila', 'filas')}` : ''}`
      L.circleMarker(toLatLng(group.point), {
        className: 'map-point',
        radius: markerRadius(count),
        color: '#2563eb',
        weight: 2,
        fillOpacity: 0.55,
      })
        .bindPopup(() => buildPopup(items, heading), { maxWidth: 300, maxHeight: 280 })
        .addTo(layer)
      bounds.extend(toLatLng(group.point))
    }

    const fit = () => fitMap(map, bounds)
    fit()
    const stopRefit = refitWhenShown(map, fit)
    return () => {
      stopRefit()
      map.closePopup()
      layer.remove()
    }
  }, [map, response, spatial, groups, pointColumn, showMbrs])

  return (
    <div className="map-view">
      <div className="map-toolbar">
        <span className="map-stat">
          <span className="map-key key-point" aria-hidden="true" />
          {plural(plotted, 'fila', 'filas')} en {plural(groups.length, 'ubicación', 'ubicaciones')}
        </span>
        {plotted < response.rows.length && (
          <span className="map-stat muted">{fmtInt(response.rows.length - plotted)} sin coordenadas</span>
        )}
        {spatial && <span className="map-stat">{describeQuery(spatial)}</span>}
        {spatial?.nodes_visited != null && (
          <span className="map-stat muted" title="Nodos del R-Tree leídos para responder la consulta">
            {plural(spatial.nodes_visited, 'nodo visitado', 'nodos visitados')}
            {spatial.leaves_visited != null && ` (${plural(spatial.leaves_visited, 'hoja', 'hojas')})`}
          </span>
        )}
        <span className="map-toolbar-spacer" />
        {pointColumns.length > 1 && (
          <label className="map-control">
            Columna
            <select className="input select" value={pointColumn} onChange={(e) => setColumnChoice(Number(e.target.value))}>
              {pointColumns.map((j) => (
                <option key={j} value={j}>
                  {response.columns[j]}
                </option>
              ))}
            </select>
          </label>
        )}
        {mbrs.length > 0 && (
          <label className="map-control check" title="Rectángulos mínimos (MBR) de los nodos del R-Tree que leyó la consulta">
            <input type="checkbox" checked={showMbrs} onChange={(e) => setShowMbrs(e.target.checked)} />
            MBR inspeccionados
            <span className="count">{fmtInt(mbrs.length)}</span>
          </label>
        )}
      </div>
      {showMbrs && mbrs.length > 0 && (
        <div className="map-legend">
          <span>
            <span className="map-key key-mbr-leaf" aria-hidden="true" />
            hojas
          </span>
          <span>
            <span className="map-key key-mbr-internal" aria-hidden="true" />
            nodos internos
          </span>
        </div>
      )}
      <div className="map-canvas" ref={containerRef} role="application" aria-label="Mapa de resultados" />
    </div>
  )
}

/** Dibuja la geometría de la consulta espacial: círculo, rectángulo o punto de referencia del k-NN. */
function drawQuery(spatial: SpatialPlan, layer: L.FeatureGroup, bounds: L.LatLngBounds): void {
  const shape = { className: 'map-query', color: '#c2410c', weight: 2, fillOpacity: 0.08, interactive: false }
  const center = isPoint(spatial.center) ? spatial.center : null

  if (spatial.kind === 'box' && Array.isArray(spatial.box) && spatial.box.length === 4) {
    const rect = boxToBounds(spatial.box)
    L.rectangle(rect, shape).addTo(layer)
    bounds.extend(rect)
    return
  }
  if (!center) return
  if (spatial.kind === 'radius' && typeof spatial.radius_km === 'number' && spatial.radius_km > 0) {
    L.circle(toLatLng(center), { ...shape, radius: spatial.radius_km * 1000 }).addTo(layer)
    bounds.extend(circleBounds(center, spatial.radius_km))
  }
  L.circleMarker(toLatLng(center), {
    className: 'map-query-center',
    radius: 5,
    color: '#c2410c',
    weight: 2,
    fillOpacity: 1,
  })
    .bindTooltip(`Punto de consulta ${formatPoint(center)}`)
    .addTo(layer)
  bounds.extend(toLatLng(center))
}

function describeQuery(spatial: SpatialPlan): string {
  if (spatial.kind === 'radius') return `radio de ${spatial.radius_km != null ? fmtNumber(spatial.radius_km, 3) : '—'} km`
  if (spatial.kind === 'box') return 'búsqueda por rectángulo'
  if (spatial.kind === 'knn') return spatial.k != null ? `${fmtInt(spatial.k)} vecinos más cercanos` : 'vecinos más cercanos'
  return String(spatial.kind)
}
