import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useEffect, useState, type RefObject } from 'react'

/** Centro inicial de los mapas: Lima. */
export const LIMA: L.LatLngTuple = [-12.0931, -77.0465]
export const LIMA_ZOOM = 11

const OSM_TILES = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'
const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>'

/** El motor guarda los puntos como [lon, lat]; Leaflet trabaja con [lat, lon]. */
export function toLatLng(point: readonly [number, number]): L.LatLngTuple {
  return [point[1], point[0]]
}

/** Esquinas de un rectángulo [lon_min, lat_min, lon_max, lat_max] en el orden de Leaflet. */
export function boxToBounds(box: readonly [number, number, number, number]): L.LatLngBounds {
  return L.latLngBounds([box[1], box[0]], [box[3], box[2]])
}

/** Rectángulo que contiene un círculo de `radiusKm` alrededor de un punto [lon, lat]. */
export function circleBounds(center: readonly [number, number], radiusKm: number): L.LatLngBounds {
  return L.latLng(toLatLng(center)).toBounds(radiusKm * 2000)
}

/** Encuadra lo dibujado; sin nada que mostrar vuelve a la vista inicial. */
export function fitMap(map: L.Map, bounds: L.LatLngBounds): void {
  if (bounds.isValid()) map.fitBounds(bounds.pad(0.12), { maxZoom: 16, animate: false })
  else map.setView(LIMA, LIMA_ZOOM, { animate: false })
}

/**
 * Vuelve a encuadrar cuando el contenedor pasa de oculto a visible (p. ej. al cambiar de
 * pestaña): mientras está oculto mide 0×0 y cualquier encuadre calculado queda mal.
 */
export function refitWhenShown(map: L.Map, fit: () => void): () => void {
  const onResize = (event: L.ResizeEvent) => {
    if (event.oldSize.x === 0 || event.oldSize.y === 0) fit()
  }
  map.on('resize', onResize)
  return () => {
    map.off('resize', onResize)
  }
}

/**
 * Crea un mapa de Leaflet con teselas de OpenStreetMap dentro del contenedor y lo destruye al
 * desmontar. Devuelve null hasta que el mapa existe, para que los efectos que dibujan dependan de él.
 */
export function useLeafletMap(containerRef: RefObject<HTMLDivElement | null>, scrollWheelZoom = true): L.Map | null {
  const [map, setMap] = useState<L.Map | null>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const instance = L.map(container, { center: LIMA, zoom: LIMA_ZOOM, scrollWheelZoom })
    instance.attributionControl.setPrefix(false)
    L.tileLayer(OSM_TILES, { maxZoom: 19, attribution: OSM_ATTRIBUTION }).addTo(instance)

    // Leaflet no se entera solo de los cambios de tamaño del contenedor (paneles, pestañas)
    const observer = new ResizeObserver(() => instance.invalidateSize({ animate: false }))
    observer.observe(container)
    setMap(instance)

    return () => {
      observer.disconnect()
      instance.remove()
      setMap(null)
    }
  }, [containerRef, scrollWheelZoom])

  return map
}

// ---------------------------------------------------------------------------
// Popups: se arman con nodos del DOM (nunca con HTML) porque muestran datos de la tabla

export interface PopupItem {
  title?: string
  /** URL de la miniatura del producto. */
  image?: string
  fields: readonly (readonly [label: string, value: string])[]
}

const POPUP_MAX_ITEMS = 6

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  node.className = className
  if (text !== undefined) node.textContent = text
  return node
}

export function buildPopup(items: readonly PopupItem[], heading?: string): HTMLElement {
  const root = element('div', 'map-popup')
  if (heading) root.append(element('div', 'map-popup-heading', heading))

  for (const item of items.slice(0, POPUP_MAX_ITEMS)) {
    const row = element('div', 'map-popup-item')
    if (item.image) {
      const img = element('img', 'map-popup-img')
      img.src = item.image
      img.alt = ''
      img.loading = 'lazy'
      row.append(img)
    }
    const body = element('div', 'map-popup-body')
    if (item.title) body.append(element('div', 'map-popup-title', item.title))
    if (item.fields.length > 0) {
      const list = element('dl', 'map-popup-fields')
      for (const [label, value] of item.fields) {
        list.append(element('dt', '', label), element('dd', '', value))
      }
      body.append(list)
    }
    row.append(body)
    root.append(row)
  }
  if (items.length > POPUP_MAX_ITEMS) {
    root.append(element('div', 'map-popup-more', `+${items.length - POPUP_MAX_ITEMS} más en este punto`))
  }
  return root
}

/** Agrupa elementos por coordenada exacta: varios productos comparten la ubicación de su tienda. */
export function groupByPoint<T>(items: readonly T[], pointOf: (item: T) => readonly [number, number] | null): { point: [number, number]; items: T[] }[] {
  const groups = new Map<string, { point: [number, number]; items: T[] }>()
  for (const item of items) {
    const point = pointOf(item)
    if (!point) continue
    const key = `${point[0]},${point[1]}`
    const group = groups.get(key)
    if (group) group.items.push(item)
    else groups.set(key, { point: [point[0], point[1]], items: [item] })
  }
  return [...groups.values()]
}

/** Radio del marcador según cuántos elementos comparten el punto. */
export function markerRadius(count: number, base = 6): number {
  return base + Math.min(6, Math.log2(Math.max(1, count)) * 1.6)
}
