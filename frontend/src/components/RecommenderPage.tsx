import L from 'leaflet'
import { useCallback, useEffect, useId, useMemo, useRef, useState, type DragEvent, type FormEvent } from 'react'
import {
  fetchAppSamples,
  fetchAppStatus,
  isAbortError,
  isMediaFile,
  mediaUrl,
  recommend,
  toApiError,
  type ApiError,
  type AppStatus,
  type HybridStrategy,
  type RecommendMethod,
  type RecommendResponse,
  type RecommendResult,
  type SampleProduct,
} from '../api'
import { fmtInt, fmtMs, fmtNumber, isPoint, plural } from '../lib/format'
import {
  buildPopup,
  circleBounds,
  fitMap,
  groupByPoint,
  markerRadius,
  refitWhenShown,
  toLatLng,
  useLeafletMap,
} from '../lib/map'
import { PlanBadge, StructureBadge } from './Badges'
import { Icon } from './Icon'
import { PlanWarning, StrategyBadge } from './PlanPanel'

const SAMPLE_COUNT = 12
const MAX_IMAGE_BYTES = 8 * 1024 * 1024
const DEFAULT_K = 12
const DEFAULT_PROBES = 5
const DEFAULT_RADIUS_KM = 3

const CATEGORIES: { value: string; label: string }[] = [
  { value: 'apparel', label: 'Ropa (apparel)' },
  { value: 'accessories', label: 'Accesorios (accessories)' },
  { value: 'footwear', label: 'Calzado (footwear)' },
  { value: 'personal care', label: 'Cuidado personal (personal care)' },
  { value: 'free items', label: 'Obsequios (free items)' },
  { value: 'sporting goods', label: 'Deportes (sporting goods)' },
  { value: 'home', label: 'Hogar (home)' },
]

interface MethodOption {
  value: RecommendMethod
  label: string
  /** Qué necesita el método para poder ejecutarse. */
  needs: 'image' | 'text'
  hint: string
}

const METHOD_OPTIONS: MethodOption[] = [
  { value: 'embedding', label: 'Embedding + IVF', needs: 'image', hint: 'Vector denso de la imagen, búsqueda aproximada en el índice IVF' },
  { value: 'bovw', label: 'BoVW + índice invertido', needs: 'image', hint: 'Palabras visuales (SIFT) buscadas en el índice invertido' },
  { value: 'exact', label: 'k-NN exacto (línea base)', needs: 'image', hint: 'Recorrido secuencial comparando todos los vectores' },
  { value: 'text', label: 'Texto (TF-IDF)', needs: 'text', hint: 'Similitud de coseno sobre la descripción' },
]

const STRATEGY_OPTIONS: { value: HybridStrategy; label: string; title: string }[] = [
  { value: 'auto', label: 'Auto', title: 'El planificador elige según la selectividad del filtro' },
  { value: 'pre', label: 'Pre-filtering', title: 'Primero los filtros escalares, luego la búsqueda por similitud' },
  { value: 'post', label: 'Post-filtering', title: 'Primero la búsqueda por similitud, luego los filtros' },
]

type QuerySource = { kind: 'file'; file: File; url: string } | { kind: 'product'; product: SampleProduct } | null

type StatusState = { status: 'loading' } | { status: 'error'; error: ApiError } | { status: 'ready'; info: AppStatus }

interface GeoPoint {
  lon: number
  lat: number
}

interface RecommenderPageProps {
  hidden: boolean
  onNetworkError(message: string): void
}

function fmtPrice(value: number | null): string {
  return value === null ? '—' : `S/ ${value.toFixed(2)}`
}

/** '' → null; texto no numérico → NaN (se reporta como error de validación). */
function parsePrice(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '') return null
  const value = Number(trimmed.replace(',', '.'))
  return Number.isFinite(value) && value >= 0 ? value : Number.NaN
}

export function RecommenderPage({ hidden, onNetworkError }: RecommenderPageProps) {
  // --- Estado del catálogo ---
  const [appStatus, setAppStatus] = useState<StatusState>({ status: 'loading' })
  const [statusSeq, setStatusSeq] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setAppStatus({ status: 'loading' })
    fetchAppStatus(controller.signal)
      .then((info) => setAppStatus({ status: 'ready', info }))
      .catch((err: unknown) => {
        if (!isAbortError(err)) setAppStatus({ status: 'error', error: toApiError(err) })
      })
    return () => controller.abort()
  }, [statusSeq])
  const loaded = appStatus.status === 'ready' && appStatus.info.loaded

  // --- Productos de ejemplo ---
  const [seed, setSeed] = useState(0)
  const [samples, setSamples] = useState<SampleProduct[]>([])
  const [samplesState, setSamplesState] = useState<'idle' | 'loading' | 'error'>('idle')
  useEffect(() => {
    if (!loaded) return
    const controller = new AbortController()
    setSamplesState('loading')
    fetchAppSamples(SAMPLE_COUNT, seed, controller.signal)
      .then((products) => {
        setSamples(products)
        setSamplesState('idle')
      })
      .catch((err: unknown) => {
        if (!isAbortError(err)) setSamplesState('error')
      })
    return () => controller.abort()
  }, [loaded, seed])

  // --- Consulta ---
  const [source, setSource] = useState<QuerySource>(null)
  const [sourceError, setSourceError] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [priceMin, setPriceMin] = useState('')
  const [priceMax, setPriceMax] = useState('')
  const [category, setCategory] = useState('')
  const [geoEnabled, setGeoEnabled] = useState(false)
  const [geoPoint, setGeoPoint] = useState<GeoPoint | null>(null)
  const [radiusKm, setRadiusKm] = useState(DEFAULT_RADIUS_KM)
  const [k, setK] = useState(DEFAULT_K)
  const [probes, setProbes] = useState(DEFAULT_PROBES)
  const [strategy, setStrategy] = useState<HybridStrategy>('auto')
  /** null = métodos por defecto según haya imagen o solo texto. */
  const [methodChoice, setMethodChoice] = useState<RecommendMethod[] | null>(null)

  // La URL de vista previa de un archivo subido se libera al reemplazarlo o al salir
  useEffect(() => {
    if (source?.kind !== 'file') return
    const url = source.url
    return () => URL.revokeObjectURL(url)
  }, [source])

  const pickFile = (file: File | undefined) => {
    if (!file) return
    if (!file.type.startsWith('image/')) {
      setSourceError('El archivo no es una imagen.')
      return
    }
    if (file.size > MAX_IMAGE_BYTES) {
      setSourceError('La imagen supera los 8 MB.')
      return
    }
    setSourceError(null)
    setSource({ kind: 'file', file, url: URL.createObjectURL(file) })
  }

  const hasImage = source !== null
  const hasText = text.trim() !== ''
  const usable = (option: MethodOption) => (option.needs === 'image' ? hasImage : hasText)
  const defaults: RecommendMethod[] = hasImage ? ['embedding', 'bovw'] : hasText ? ['text'] : []
  const methods = METHOD_OPTIONS.filter((o) => usable(o) && (methodChoice ?? defaults).includes(o.value)).map((o) => o.value)

  const toggleMethod = (value: RecommendMethod, checked: boolean) => {
    const current = new Set<RecommendMethod>(methods)
    if (checked) current.add(value)
    else current.delete(value)
    setMethodChoice(METHOD_OPTIONS.map((o) => o.value).filter((v) => current.has(v)))
  }

  const min = parsePrice(priceMin)
  const max = parsePrice(priceMax)
  const geoActive = geoEnabled && geoPoint !== null
  let invalid: string | null = null
  if (!hasImage && !hasText) invalid = 'Sube una foto, elige un producto o escribe una descripción.'
  else if (Number.isNaN(min) || Number.isNaN(max)) invalid = 'El precio debe ser un número mayor o igual a 0.'
  else if (min !== null && max !== null && min > max) invalid = 'El precio mínimo no puede superar al máximo.'
  else if (geoEnabled && geoPoint === null) invalid = 'Haz clic en el mapa para fijar el punto de cercanía.'
  else if (methods.length === 0) invalid = 'Marca al menos un método para comparar.'

  // --- Ejecución ---
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [response, setResponse] = useState<RecommendResponse | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => () => abortRef.current?.abort(), [])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || invalid !== null || !loaded) return
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    setError(null)
    try {
      const result = await recommend(
        {
          image: source?.kind === 'file' ? source.file : null,
          product_id: source?.kind === 'product' ? source.product.id : null,
          text,
          price_min: min,
          price_max: max,
          lon: geoActive ? geoPoint.lon : null,
          lat: geoActive ? geoPoint.lat : null,
          radius_km: geoActive ? radiusKm : null,
          category,
          k,
          probes,
          strategy,
          methods,
        },
        controller.signal,
      )
      setResponse(result)
    } catch (err) {
      if (isAbortError(err)) return
      const apiError = toApiError(err)
      setError(apiError)
      if (apiError.kind === 'network') onNetworkError(apiError.message)
    } finally {
      if (abortRef.current === controller) setBusy(false)
    }
  }

  const pickOnMap = useCallback((point: GeoPoint) => {
    setGeoPoint(point)
    setGeoEnabled(true)
  }, [])

  return (
    <main className="recommender" hidden={hidden}>
      <section className="panel rec-query" aria-labelledby="rec-query-title">
        <header className="panel-header">
          <h2 className="panel-title" id="rec-query-title">
            <Icon name="search" />
            Consulta
          </h2>
        </header>
        <form className="rec-form" onSubmit={(e) => void submit(e)}>
          <div className="rec-form-scroll">
            <fieldset className="rec-group">
              <legend className="section-title">Imagen de consulta</legend>
              <ImagePicker source={source} onFile={pickFile} onClear={() => setSource(null)} />
              {sourceError && (
                <div className="inline-alert" role="alert">
                  <Icon name="alert" size={14} />
                  <span>{sourceError}</span>
                </div>
              )}
              <div className="section-head">
                <span className="rec-label">O elige un producto del catálogo</span>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  onClick={() => setSeed((value) => value + 1)}
                  disabled={!loaded || samplesState === 'loading'}
                >
                  <Icon name="refresh" size={13} className={samplesState === 'loading' ? 'spin' : undefined} />
                  Otros ejemplos
                </button>
              </div>
              <SampleGrid
                samples={samples}
                state={samplesState}
                loaded={loaded}
                selectedId={source?.kind === 'product' ? source.product.id : null}
                onPick={(product) => {
                  setSourceError(null)
                  setSource({ kind: 'product', product })
                }}
              />
            </fieldset>

            <fieldset className="rec-group">
              <legend className="section-title">Descripción (opcional)</legend>
              <input
                className="input rec-input"
                type="text"
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="blue running shoes"
                aria-label="Descripción del producto"
                maxLength={200}
              />
              <p className="muted-note">En inglés: el catálogo está en inglés. Con una imagen, la descripción actúa como filtro.</p>
            </fieldset>

            <fieldset className="rec-group">
              <legend className="section-title">Filtros</legend>
              <div className="rec-row">
                <label className="rec-field">
                  <span className="rec-label">Precio mínimo (S/)</span>
                  <input className="input rec-input" inputMode="decimal" value={priceMin} onChange={(e) => setPriceMin(e.target.value)} placeholder="0" />
                </label>
                <label className="rec-field">
                  <span className="rec-label">Precio máximo (S/)</span>
                  <input className="input rec-input" inputMode="decimal" value={priceMax} onChange={(e) => setPriceMax(e.target.value)} placeholder="sin tope" />
                </label>
              </div>
              <label className="rec-field">
                <span className="rec-label">Categoría</span>
                <select className="input select rec-input" value={category} onChange={(e) => setCategory(e.target.value)}>
                  <option value="">Todas</option>
                  {CATEGORIES.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </label>

              <div className="section-head">
                <label className="rec-check">
                  <input type="checkbox" checked={geoEnabled} onChange={(e) => setGeoEnabled(e.target.checked)} />
                  Filtrar por cercanía
                </label>
                {geoPoint && (
                  <span className="muted-note mono" title="lon, lat">
                    {fmtNumber(geoPoint.lon, 4)}, {fmtNumber(geoPoint.lat, 4)}
                  </span>
                )}
              </div>
              <LocationPicker point={geoPoint} radiusKm={radiusKm} active={geoEnabled} onPick={pickOnMap} />
              <label className="rec-slider">
                <span className="rec-label">Radio</span>
                <input
                  type="range"
                  min={0.5}
                  max={20}
                  step={0.5}
                  value={radiusKm}
                  disabled={!geoEnabled}
                  onChange={(e) => setRadiusKm(Number(e.target.value))}
                />
                <span className="rec-slider-value">{fmtNumber(radiusKm, 1)} km</span>
              </label>
              <p className="muted-note">
                {geoEnabled
                  ? 'Haz clic en el mapa para mover el punto. Solo se recomiendan productos de tiendas dentro del radio.'
                  : 'Haz clic en el mapa para fijar un punto y activar el filtro.'}
              </p>
            </fieldset>

            <details className="rec-group rec-advanced">
              <summary>
                <Icon name="chevronRight" size={14} className="details-caret" />
                <span className="section-title">Parámetros avanzados</span>
              </summary>
              <label className="rec-slider">
                <span className="rec-label" title="Cantidad de productos a recomendar">
                  k
                </span>
                <input type="range" min={6} max={48} step={1} value={k} onChange={(e) => setK(Number(e.target.value))} />
                <span className="rec-slider-value">{k}</span>
              </label>
              <label className="rec-slider">
                <span className="rec-label" title="Celdas del IVF que se exploran: más celdas, mejor recall y más lecturas">
                  ivf.probes
                </span>
                <input type="range" min={1} max={50} step={1} value={probes} onChange={(e) => setProbes(Number(e.target.value))} />
                <span className="rec-slider-value">{probes}</span>
              </label>
              <div className="rec-field">
                <span className="rec-label" id="rec-strategy-label">
                  Estrategia de filtrado
                </span>
                <div className="segmented rec-segmented" role="group" aria-labelledby="rec-strategy-label">
                  {STRATEGY_OPTIONS.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      aria-pressed={strategy === option.value}
                      onClick={() => setStrategy(option.value)}
                      title={option.title}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="rec-field">
                <span className="rec-label">Métodos a comparar</span>
                {METHOD_OPTIONS.map((option) => (
                  <label
                    key={option.value}
                    className={`rec-check${usable(option) ? '' : ' is-disabled'}`}
                    title={usable(option) ? option.hint : option.needs === 'image' ? 'Necesita una imagen de consulta' : 'Necesita una descripción'}
                  >
                    <input
                      type="checkbox"
                      checked={methods.includes(option.value)}
                      disabled={!usable(option)}
                      onChange={(e) => toggleMethod(option.value, e.target.checked)}
                    />
                    <span className={`method-dot method-${option.value}`} aria-hidden="true" />
                    {option.label}
                  </label>
                ))}
              </div>
            </details>
          </div>

          <div className="rec-submit">
            {invalid !== null && (hasImage || hasText) && <p className="rec-invalid">{invalid}</p>}
            <button
              type="submit"
              className="btn btn-primary rec-submit-btn"
              disabled={busy || invalid !== null || !loaded}
              title={invalid ?? (loaded ? undefined : 'El catálogo de productos no está cargado')}
            >
              {busy ? <span className="spinner" aria-hidden="true" /> : <Icon name="search" size={14} />}
              {busy ? 'Buscando…' : 'Recomendar'}
            </button>
          </div>
        </form>
      </section>

      <section className="panel rec-results" aria-labelledby="rec-results-title" aria-busy={busy}>
        <header className="panel-header">
          <h2 className="panel-title" id="rec-results-title">
            <Icon name="image" />
            Recomendaciones
          </h2>
          <div className="panel-actions">
            {busy && (
              <span className="summary-chip">
                <span className="spinner" aria-hidden="true" />
                Ejecutando consultas…
              </span>
            )}
            {appStatus.status === 'ready' && appStatus.info.info && (
              <span className="summary-chip" title={`Tabla ${appStatus.info.table}`}>
                {plural(appStatus.info.info.records, 'producto', 'productos')}
              </span>
            )}
          </div>
        </header>
        {busy && <div className="progress-bar" aria-hidden="true" />}
        <div className={`panel-body rec-results-body${busy ? ' is-busy' : ''}`}>
          {error && (
            <div className="error-view rec-error" role="alert">
              <div className="error-head">
                <Icon name="alert" size={18} />
                <span className="error-type">{error.errorType}</span>
                {error.status !== null && <span className="error-status">HTTP {error.status}</span>}
              </div>
              <p className="error-message">{error.message}</p>
            </div>
          )}
          {appStatus.status === 'loading' && !response && (
            <div className="empty-state">
              <span className="spinner spinner-lg" aria-hidden="true" />
              <p className="empty-title">Consultando el catálogo…</p>
            </div>
          )}
          {appStatus.status === 'error' && !response && (
            <div className="empty-state">
              <Icon name="alert" size={28} className="empty-icon is-danger" />
              <p className="empty-title">No se pudo consultar el estado de la aplicación</p>
              <p className="empty-text">{appStatus.error.message}</p>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => setStatusSeq((n) => n + 1)}>
                <Icon name="refresh" size={14} />
                Reintentar
              </button>
            </div>
          )}
          {appStatus.status === 'ready' && !appStatus.info.loaded && (
            <div className="empty-state">
              <Icon name="database" size={28} className="empty-icon" />
              <p className="empty-title">El catálogo de productos no está cargado</p>
              <p className="empty-text">
                El recomendador trabaja sobre la tabla <code>{appStatus.info.table}</code>. Para crearla con sus índices,
                ejecuta en la raíz del proyecto:
              </p>
              <pre className="rec-command">python data/prepare_fashion.py --load</pre>
              <p className="empty-text">Luego reinicia el backend y vuelve a comprobar.</p>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => setStatusSeq((n) => n + 1)}>
                <Icon name="refresh" size={14} />
                Volver a comprobar
              </button>
            </div>
          )}
          {appStatus.status === 'ready' && appStatus.info.loaded && !response && !error && <Welcome info={appStatus.info} />}
          {response && <Results response={response} />}
        </div>
      </section>
    </main>
  )
}

// ---------------------------------------------------------------------------
// Columna de consulta

interface ImagePickerProps {
  source: QuerySource
  onFile(file: File | undefined): void
  onClear(): void
}

function ImagePicker({ source, onFile, onClear }: ImagePickerProps) {
  const [dragging, setDragging] = useState(false)
  const inputId = useId()

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault()
    setDragging(false)
    onFile(event.dataTransfer.files[0])
  }

  return (
    <div className="rec-image">
      <label
        htmlFor={inputId}
        className={`dropzone${dragging ? ' is-dragging' : ''}${source ? ' has-image' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        {source ? (
          <>
            <img
              className={`dropzone-preview${source.kind === 'product' ? ' product-photo' : ''}`}
              src={source.kind === 'file' ? source.url : mediaUrl(source.product.imagen)}
              alt="Imagen de consulta"
            />
            <span className="dropzone-info">
              <strong>{source.kind === 'file' ? source.file.name : source.product.titulo}</strong>
              <span className="muted">
                {source.kind === 'file'
                  ? `Foto subida · ${fmtInt(Math.ceil(source.file.size / 1024))} KB`
                  : `Producto ${source.product.id} · ${source.product.tipo} · ${fmtPrice(source.product.precio)}`}
              </span>
              <span className="dropzone-hint">Suelta otra foto aquí o haz clic para cambiarla</span>
            </span>
          </>
        ) : (
          <>
            <Icon name="upload" size={22} className="dropzone-icon" />
            <span className="dropzone-info">
              <strong>Sube la foto de una prenda</strong>
              <span className="dropzone-hint">Arrástrala aquí o haz clic para elegirla (JPG o PNG, hasta 8 MB)</span>
            </span>
          </>
        )}
      </label>
      <input
        id={inputId}
        className="sr-only"
        type="file"
        accept="image/*"
        onChange={(e) => {
          onFile(e.target.files?.[0])
          // Permite volver a elegir el mismo archivo después de quitarlo
          e.target.value = ''
        }}
      />
      {source && (
        <button type="button" className="btn btn-ghost btn-xs rec-image-clear" onClick={onClear}>
          <Icon name="x" size={13} />
          Quitar imagen
        </button>
      )}
    </div>
  )
}

interface SampleGridProps {
  samples: readonly SampleProduct[]
  state: 'idle' | 'loading' | 'error'
  loaded: boolean
  selectedId: number | null
  onPick(product: SampleProduct): void
}

function SampleGrid({ samples, state, loaded, selectedId, onPick }: SampleGridProps) {
  if (!loaded) return <p className="muted-note">Disponible cuando el catálogo esté cargado.</p>
  if (state === 'error') return <p className="muted-note">No se pudieron cargar los productos de ejemplo.</p>
  if (samples.length === 0) {
    return <p className="muted-note">{state === 'loading' ? 'Cargando ejemplos…' : 'No hay ejemplos en este rango; prueba con otros.'}</p>
  }
  return (
    <div className={`sample-grid${state === 'loading' ? ' is-busy' : ''}`}>
      {samples.map((product) => (
        <button
          key={product.id}
          type="button"
          className="sample"
          aria-pressed={product.id === selectedId}
          onClick={() => onPick(product)}
          title={`${product.titulo} · ${product.tipo} · ${fmtPrice(product.precio)}`}
        >
          <img className="product-photo" src={mediaUrl(product.imagen)} alt={product.titulo} loading="lazy" />
        </button>
      ))}
    </div>
  )
}

interface LocationPickerProps {
  point: GeoPoint | null
  radiusKm: number
  active: boolean
  onPick(point: GeoPoint): void
}

/** Mini-mapa: un clic fija el centro del filtro de cercanía. */
function LocationPicker({ point, radiusKm, active, onPick }: LocationPickerProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const map = useLeafletMap(containerRef, false)

  useEffect(() => {
    if (!map) return
    const onClick = (event: L.LeafletMouseEvent) => {
      const { lng, lat } = event.latlng.wrap()
      onPick({ lon: Number(lng.toFixed(6)), lat: Number(lat.toFixed(6)) })
    }
    map.on('click', onClick)
    return () => {
      map.off('click', onClick)
    }
  }, [map, onPick])

  useEffect(() => {
    if (!map || !point) return
    const center: [number, number] = [point.lon, point.lat]
    const layer = L.featureGroup().addTo(map)
    const state = active ? '' : ' is-off'
    L.circle(toLatLng(center), {
      className: `map-query${state}`,
      radius: radiusKm * 1000,
      color: '#c2410c',
      weight: 2,
      fillOpacity: 0.08,
      interactive: false,
    }).addTo(layer)
    L.circleMarker(toLatLng(center), {
      className: `map-query-center${state}`,
      radius: 5,
      color: '#c2410c',
      weight: 2,
      fillOpacity: 1,
      interactive: false,
    }).addTo(layer)
    // Solo se mueve la vista si el círculo no entra completo en lo que ya se ve
    const bounds = circleBounds(center, radiusKm)
    if (!map.getBounds().contains(bounds)) map.fitBounds(bounds.pad(0.1), { animate: false })
    return () => {
      layer.remove()
    }
  }, [map, point, radiusKm, active])

  return <div className="map-canvas rec-picker" ref={containerRef} role="application" aria-label="Mapa para fijar el punto de cercanía" />
}

// ---------------------------------------------------------------------------
// Resultados

function Welcome({ info }: { info: AppStatus }) {
  return (
    <div className="empty-state">
      <Icon name="image" size={28} className="empty-icon" />
      <p className="empty-title">Recomendador de productos por imagen y texto</p>
      <p className="empty-text">
        Sube la foto de una prenda o elige un producto del catálogo, agrega filtros de precio y cercanía, y compara las
        recomendaciones de cada índice con su plan de ejecución y sus lecturas de disco.
      </p>
      {info.info && info.info.indexes.length > 0 && (
        <ul className="rec-indexes" aria-label="Índices de la tabla de productos">
          {info.info.indexes.map((index) => (
            <li key={index.name} title={index.name}>
              <StructureBadge structure={index.type} />
              <code>{index.column}</code>
            </li>
          ))}
        </ul>
      )}
      {!info.models && (
        <div className="inline-alert is-warn" role="status">
          <Icon name="alert" size={14} />
          <span>Faltan los modelos de extracción de características: solo funcionará la búsqueda por texto.</span>
        </div>
      )}
      {!info.media && (
        <div className="inline-alert is-warn" role="status">
          <Icon name="alert" size={14} />
          <span>No se encontró la carpeta de imágenes de los productos: las miniaturas no se mostrarán.</span>
        </div>
      )}
    </div>
  )
}

interface Product {
  id: string
  title: string
  type: string
  price: number | null
  store: string
  point: [number, number] | null
  image: string | null
  score: number | null
}

interface MethodResult {
  result: RecommendResult
  products: Product[]
}

function toProducts(result: RecommendResult): Product[] {
  const at = (name: string) => result.columns.indexOf(name)
  const col = {
    id: at('id'),
    title: at('titulo'),
    type: at('tipo'),
    price: at('precio'),
    store: at('tienda'),
    point: at('ubicacion'),
    image: at('imagen'),
    score: at(result.score_column),
  }
  const text = (value: unknown) => (typeof value === 'string' ? value : value == null ? '' : String(value))
  const num = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : null)
  return result.rows.map((row, i) => {
    const point = row[col.point]
    const image = row[col.image]
    return {
      id: col.id >= 0 ? text(row[col.id]) : `fila-${i}`,
      title: text(row[col.title]),
      type: text(row[col.type]),
      price: num(row[col.price]),
      store: text(row[col.store]),
      point: isPoint(point) ? point : null,
      image: isMediaFile(image) ? image : null,
      score: num(row[col.score]),
    }
  })
}

function Results({ response }: { response: RecommendResponse }) {
  const mapRef = useRef<HTMLDivElement>(null)
  const methods = useMemo<MethodResult[]>(
    () => response.results.map((result) => ({ result, products: toProducts(result) })),
    [response],
  )
  // En cuántos métodos aparece cada producto, para resaltar las coincidencias
  const appearances = useMemo(() => {
    const counts = new Map<string, number>()
    for (const { products } of methods) {
      for (const id of new Set(products.map((p) => p.id))) counts.set(id, (counts.get(id) ?? 0) + 1)
    }
    return counts
  }, [methods])
  const compared = methods.filter((m) => m.result.error === null).length
  const shared = compared >= 2 ? [...appearances.values()].filter((n) => n >= 2).length : 0
  const hasPoints = methods.some((m) => m.products.some((p) => p.point !== null))
  const { features, query } = response
  const filters = query.filters

  return (
    <div className="rec-content">
      <div className="rec-summary">
        {features && (
          <span className="rec-features" title={`Imagen de ${features.width}×${features.height} px`}>
            <strong>Extracción:</strong> embedding {fmtMs(features.embedding_ms)} ms · BoVW {fmtMs(features.bovw_ms)} ms ·{' '}
            {plural(features.keypoints, 'punto SIFT', 'puntos SIFT')} · {plural(features.visual_words, 'palabra visual', 'palabras visuales')}
          </span>
        )}
        <span className="rec-chips">
          <span className="chip">k = {query.k}</span>
          <span className="chip">ivf.probes = {query.probes}</span>
          <span className="chip">estrategia: {query.strategy}</span>
          {query.text && <span className="chip">texto: {query.text}</span>}
          {(filters.price_min !== null || filters.price_max !== null) && (
            <span className="chip">
              precio: {filters.price_min !== null ? fmtPrice(filters.price_min) : '…'} – {filters.price_max !== null ? fmtPrice(filters.price_max) : '…'}
            </span>
          )}
          {filters.category && <span className="chip">categoría: {filters.category}</span>}
          {filters.radius_km !== null && <span className="chip">a {fmtNumber(filters.radius_km, 1)} km o menos</span>}
          {shared > 0 && <span className="chip chip-shared">{plural(shared, 'producto en común', 'productos en común')}</span>}
        </span>
        {hasPoints && (
          <button type="button" className="btn btn-ghost btn-xs" onClick={() => mapRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })}>
            <Icon name="map" size={13} />
            Ver tiendas en el mapa
          </button>
        )}
      </div>

      {methods.length === 0 ? (
        <div className="empty-state compact">
          <p className="empty-title">El backend no devolvió resultados</p>
        </div>
      ) : (
        <div className={`rec-methods${methods.length > 1 ? ' is-compare' : ''}`} style={{ ['--method-count' as string]: Math.min(methods.length, 4) }}>
          {methods.map((m) => (
            <MethodCard key={m.result.method} data={m} appearances={appearances} compared={compared} />
          ))}
        </div>
      )}

      {hasPoints && (
        <div ref={mapRef}>
          <StoresMap methods={methods} filters={filters} />
        </div>
      )}
    </div>
  )
}

interface MethodCardProps {
  data: MethodResult
  appearances: ReadonlyMap<string, number>
  compared: number
}

function MethodCard({ data, appearances, compared }: MethodCardProps) {
  const { result, products } = data
  const { plan, metrics } = result
  const scoreLabel = result.score_column || 'puntuación'

  return (
    <article className={`method-card method-${result.method}`} aria-label={result.label}>
      <header className="method-head">
        <h3 className="method-title">
          <span className="method-dot" aria-hidden="true" />
          {result.label}
        </h3>
        <div className="method-badges">
          {plan && <PlanBadge type={plan.type} size="xs" />}
          {plan?.strategy && <StrategyBadge strategy={plan.strategy} size="xs" />}
          {plan?.index && <code className="method-index">{plan.index}</code>}
        </div>
      </header>

      {metrics && (
        <dl className="method-metrics">
          <div title="Páginas leídas de disco en total">
            <dt>Lecturas</dt>
            <dd>{fmtInt(metrics.disk_reads)}</dd>
          </div>
          <div title="Páginas leídas en archivos de índice">
            <dt>En índice</dt>
            <dd>{metrics.io ? fmtInt(metrics.io.index_reads) : '—'}</dd>
          </div>
          <div title="Páginas leídas en el archivo de datos de la tabla">
            <dt>En tabla</dt>
            <dd>{metrics.io ? fmtInt(metrics.io.table_reads) : '—'}</dd>
          </div>
          <div title="Tiempo de ejecución de la consulta">
            <dt>Ejecución</dt>
            <dd>
              {fmtMs(metrics.exec_ms)}
              <span className="kpi-unit">ms</span>
            </dd>
          </div>
        </dl>
      )}

      {plan?.warning && <PlanWarning message={plan.warning} />}
      {result.error && (
        <div className="inline-alert" role="alert">
          <Icon name="alert" size={14} />
          <span>{result.error}</span>
        </div>
      )}

      {result.sql && (
        <details className="method-sql">
          <summary>
            <Icon name="chevronRight" size={14} className="details-caret" />
            SQL ejecutado
          </summary>
          <pre>{result.sql}</pre>
        </details>
      )}

      {!result.error && products.length === 0 && <p className="muted-note">Ningún producto cumple los filtros.</p>}
      {products.length > 0 && (
        <ol className="product-grid">
          {products.map((product, i) => {
            const count = appearances.get(product.id) ?? 1
            const isShared = compared >= 2 && count >= 2
            return (
              <li key={`${product.id}-${i}`} className={`product${isShared ? ' is-shared' : ''}`}>
                <div className="product-media">
                  {product.image ? (
                    <img className="product-photo" src={mediaUrl(product.image)} alt={product.title} loading="lazy" />
                  ) : (
                    <span className="product-noimg">
                      <Icon name="image" size={22} />
                    </span>
                  )}
                  <span className="product-rank">{i + 1}</span>
                  {isShared && (
                    <span className="product-shared" title="Este producto también aparece en otro método">
                      {compared === 2 ? 'en ambos' : `en ${count} métodos`}
                    </span>
                  )}
                </div>
                <div className="product-body">
                  <span className="product-title" title={product.title}>
                    {product.title}
                  </span>
                  <span className="product-meta">{product.type}</span>
                  <span className="product-price">{fmtPrice(product.price)}</span>
                  <span className="product-meta" title={product.store}>
                    {product.store}
                  </span>
                  <span className="product-score" title={`${scoreLabel} (${result.score_column === 'distancia' ? 'menor es más parecido' : 'mayor es más parecido'})`}>
                    {scoreLabel} <strong>{product.score !== null ? product.score.toFixed(4) : '—'}</strong>
                  </span>
                </div>
              </li>
            )
          })}
        </ol>
      )}
    </article>
  )
}

interface StoresMapProps {
  methods: readonly MethodResult[]
  filters: RecommendResponse['query']['filters']
}

/** Tiendas de los productos recomendados: un anillo por método en cada ubicación. */
function StoresMap({ methods, filters }: StoresMapProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const map = useLeafletMap(containerRef, false)
  const center = useMemo<[number, number] | null>(
    () => (filters.lon !== null && filters.lat !== null ? [filters.lon, filters.lat] : null),
    [filters.lon, filters.lat],
  )
  const radius = filters.radius_km

  useEffect(() => {
    if (!map) return
    const layer = L.featureGroup().addTo(map)
    const bounds = L.latLngBounds([])

    if (center && radius !== null && radius > 0) {
      L.circle(toLatLng(center), { className: 'map-query', radius: radius * 1000, color: '#c2410c', weight: 2, fillOpacity: 0.08, interactive: false }).addTo(layer)
      L.circleMarker(toLatLng(center), { className: 'map-query-center', radius: 5, color: '#c2410c', weight: 2, fillOpacity: 1, interactive: false }).addTo(layer)
      bounds.extend(circleBounds(center, radius))
    }

    // El primer método dibuja el anillo más grande; los siguientes quedan dentro y siguen visibles
    methods.forEach(({ result, products }, index) => {
      const extra = (methods.length - 1 - index) * 4
      for (const group of groupByPoint(products, (p) => p.point)) {
        const store = group.items[0]?.store ?? ''
        const heading = `${store || 'Tienda'} · ${result.label} · ${plural(group.items.length, 'producto', 'productos')}`
        const items = group.items.map((p) => ({
          title: p.title,
          image: p.image ? mediaUrl(p.image) : undefined,
          fields: [
            ['precio', fmtPrice(p.price)],
            [result.score_column || 'puntuación', p.score !== null ? p.score.toFixed(4) : '—'],
          ] as const,
        }))
        L.circleMarker(toLatLng(group.point), {
          className: `map-method method-${result.method}`,
          radius: markerRadius(group.items.length, 5) + extra,
          color: '#2a78d6',
          weight: 2,
          fillOpacity: 0.45,
        })
          .bindPopup(() => buildPopup(items, heading), { maxWidth: 300, maxHeight: 280 })
          .addTo(layer)
        bounds.extend(toLatLng(group.point))
      }
    })

    const fit = () => fitMap(map, bounds)
    fit()
    const stopRefit = refitWhenShown(map, fit)
    return () => {
      stopRefit()
      map.closePopup()
      layer.remove()
    }
  }, [map, methods, center, radius])

  return (
    <section className="rec-map" aria-label="Mapa de tiendas">
      <div className="section-head">
        <h3 className="section-title">Tiendas de los productos recomendados</h3>
        <div className="map-legend">
          {methods.map(({ result }) => (
            <span key={result.method}>
              <span className={`method-dot method-${result.method}`} aria-hidden="true" />
              {result.label}
            </span>
          ))}
          {center && radius !== null && (
            <span>
              <span className="map-key key-query" aria-hidden="true" />
              filtro de {fmtNumber(radius, 1)} km
            </span>
          )}
        </div>
      </div>
      <div className="map-canvas rec-stores" ref={containerRef} role="application" aria-label="Mapa de tiendas de los productos recomendados" />
    </section>
  )
}
