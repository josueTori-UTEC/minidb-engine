// Cliente tipado de la API REST de MiniDB. El contrato vive en docs/api.md.

const API: string = import.meta.env.VITE_API_URL ?? ''
const BASE = API.replace(/\/+$/, '')

/** Destino de las peticiones, para mostrarlo en la interfaz. */
export const API_TARGET = BASE === '' ? 'mismo origen (/api)' : BASE

// ---------------------------------------------------------------------------
// Tipos del contrato
// ---------------------------------------------------------------------------

/** Valores conocidos + cualquier otro string (el backend puede agregar tipos). */
type Open<T extends string> = T | (string & {})

export type StatementType = Open<
  | 'SELECT'
  | 'INSERT'
  | 'DELETE'
  | 'CREATE TABLE'
  | 'CREATE INDEX'
  | 'DROP TABLE'
  | 'DROP INDEX'
  | 'COPY'
  | 'EXPLAIN'
  | 'SET'
>
export type PlanType = Open<
  | 'SeqScan'
  | 'IndexScan'
  | 'IndexRangeScan'
  | 'BinarySearch'
  | 'SeqFileRangeScan'
  | 'Insert'
  | 'RTreeRadiusScan'
  | 'RTreeWindowScan'
  | 'RTreeKNN'
  | 'InvertedIndexScan'
  | 'IVFScan'
>
export type Organization = Open<'HEAP' | 'SEQUENTIAL'>
export type IndexType = Open<'BTREE' | 'HASH' | 'RTREE' | 'INVERTED' | 'IVF'>
export type Structure = Open<'HEAP' | 'SEQUENTIAL' | 'BTREE' | 'HASH' | 'RTREE' | 'INVERTED' | 'IVF'>
export type ColumnType = Open<'INT' | 'FLOAT' | 'CHAR' | 'VARCHAR' | 'POINT' | 'VECTOR' | 'TEXT'>
/** Estrategia de una consulta híbrida (filtro escalar + búsqueda espacial, textual o vectorial). */
export type FilterStrategy = Open<'pre-filter' | 'post-filter'>

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue }
/** Una columna POINT llega como [lon, lat] y una VECTOR como un arreglo de N números. */
export type CellValue = string | number | boolean | null | number[]

export interface IoFile {
  file: string
  role: Open<'table' | 'index' | 'other'>
  owner: string
  reads: number
  writes: number
}

/** Lecturas y escrituras de páginas separadas por archivo: índices vs. tabla de datos. */
export interface IoBreakdown {
  index_reads: number
  index_writes: number
  table_reads: number
  table_writes: number
  other_reads: number
  other_writes: number
  files: IoFile[]
}

export interface Metrics {
  disk_reads: number
  disk_writes: number
  parse_ms: number
  exec_ms: number
  total_ms: number
  /** Ausente en respuestas de versiones anteriores del backend. */
  io?: IoBreakdown
}

export interface PlanCandidate {
  type: PlanType
  structure: Structure
  index: string | null
  estimated_io: number | null
  chosen: boolean
  strategy?: FilterStrategy | null
}

/** Rectángulo mínimo de un nodo del R-Tree: [lon_min, lat_min, lon_max, lat_max]. */
export interface SpatialMbr {
  page_id: number
  level: number
  leaf: boolean
  mbr: [number, number, number, number]
}

export interface SpatialPlan {
  kind: Open<'radius' | 'box' | 'knn'>
  center?: [number, number] | null
  radius_km?: number | null
  box?: [number, number, number, number] | null
  k?: number | null
  candidates?: number
  nodes_visited?: number
  leaves_visited?: number
  mbrs?: SpatialMbr[]
}

export interface TextPlanTerm {
  term: string
  df: number
  idf: number
  posting_pages: number
}

export interface TextPlan {
  mode: Open<'boolean' | 'ranked'>
  scoring?: string | null
  matched?: number
  rebuilt?: boolean
  terms?: TextPlanTerm[]
}

export interface VectorPlan {
  metric?: string | null
  probes?: number
  lists?: number
  probed?: number[]
  dir_pages?: number
  list_pages?: number
  scanned?: number
  fetched?: number
  rebuilt?: boolean
}

export interface Plan {
  type: PlanType
  table: string
  structure: Structure
  index: string | null
  column: string | null
  predicate: string | null
  filter: string | null
  estimated_io: number | null
  detail: string | null
  planner: string | null
  candidates: PlanCandidate[]
  strategy?: FilterStrategy | null
  order_by?: string | null
  limit?: number | null
  warning?: string | null
  spatial?: SpatialPlan | null
  text?: TextPlan | null
  vector?: VectorPlan | null
  prefilter_rids?: number | null
}

export interface StatementSummary {
  sql: string
  statement: StatementType
  message: string
  metrics: Metrics
}

/** 'rules': reglas del enunciado (por defecto); 'cost': menor costo estimado de I/O. */
export type PlannerMode = 'rules' | 'cost'

export interface QueryRequest {
  sql: string
  page?: number
  page_size?: number
  planner?: PlannerMode
}

export interface QueryResponse {
  statement: StatementType
  columns: string[]
  rows: CellValue[][]
  total_rows: number
  page: number
  page_size: number
  message: string
  plan: Plan | null
  metrics: Metrics
  statements: StatementSummary[]
}

export interface ErrorPosition {
  line: number
  column: number
  offset: number
}

export interface ColumnInfo {
  name: string
  type: ColumnType
  size: number | null
  primary_key: boolean
}

export interface IndexInfo {
  name: string
  column: string
  type: IndexType
  unique?: boolean
  height: number | null
  global_depth: number | null
  page_count: number
  entry_count: number
  file?: string
  files?: string[]
  /** Opciones del WITH (...) con las que se creó el índice. */
  options?: Record<string, JsonValue> | null
  /** Metadatos propios de cada tipo (R-Tree, invertido, IVF). */
  details?: Record<string, JsonValue> | null
}

export interface TableInfo {
  name: string
  organization: Organization
  page_size: number
  primary_key: string | null
  columns: ColumnInfo[]
  record_size: number
  records_per_page: number
  record_count: number
  page_count: number
  overflow_page_count: number | null
  indexes: IndexInfo[]
}

export interface ReorganizeSnapshot {
  main_pages: number
  overflow_pages: number
  record_count: number
}

export interface ReorganizeResponse {
  table: string
  message: string
  before: ReorganizeSnapshot
  after: ReorganizeSnapshot
  metrics: Metrics
}

export interface TableFile {
  key: string
  kind: string
  path: string
  num_pages: number
  page_size: number
}

export interface PageHeader {
  page_id: number
  page_type: number
  page_type_name: string
  flags: number
  record_count: number
  free_space_offset: number
  next_page_id: number
  prev_page_id: number
  aux_page_id: number
  [field: string]: JsonValue
}

export interface PageDump {
  table: string
  file: string
  path: string
  page_id: number
  page_size: number
  num_pages: number
  header: PageHeader
  details: JsonValue
  hexdump: string
}

export interface HealthInfo {
  status: string
  data_dir?: string
  tables?: number
}

// --- Aplicación: recomendador de productos ---

export type RecommendMethod = Open<'embedding' | 'bovw' | 'exact' | 'text'>
export type HybridStrategy = 'auto' | 'pre' | 'post'

export interface AppStatus {
  table: string
  loaded: boolean
  models: boolean
  media: boolean
  info: { records: number; pages: number; indexes: { name: string; type: IndexType; column: string }[] } | null
  methods: Record<string, string>
}

export interface SampleProduct {
  id: number
  titulo: string
  categoria: string
  tipo: string
  precio: number
  imagen: string
}

/** Campos del formulario de POST /api/app/recommend; se omiten los que no se envían. */
export interface RecommendParams {
  image?: File | null
  product_id?: number | null
  text?: string
  price_min?: number | null
  price_max?: number | null
  lon?: number | null
  lat?: number | null
  radius_km?: number | null
  category?: string
  k?: number
  probes?: number
  strategy?: HybridStrategy
  methods?: readonly RecommendMethod[]
}

export interface RecommendFeatures {
  keypoints: number
  embedding_ms: number
  bovw_ms: number
  width: number
  height: number
  visual_words: number
}

export interface RecommendResult {
  method: RecommendMethod
  label: string
  sql: string
  score_column: string
  columns: string[]
  rows: CellValue[][]
  total_rows: number
  plan: Plan | null
  metrics: Metrics | null
  error: string | null
}

export interface RecommendResponse {
  query: {
    text: string
    has_image: boolean
    k: number
    probes: number
    strategy: string
    product_id: number | null
    filters: {
      price_min: number | null
      price_max: number | null
      lon: number | null
      lat: number | null
      radius_km: number | null
      category: string
    }
  }
  features: RecommendFeatures | null
  results: RecommendResult[]
}

// ---------------------------------------------------------------------------
// Errores
// ---------------------------------------------------------------------------

/** network: no hubo respuesta útil del backend; http: el backend respondió con error. */
export type ApiErrorKind = 'network' | 'http' | 'invalid'

export class ApiError extends Error {
  override name = 'ApiError'
  readonly kind: ApiErrorKind
  readonly status: number | null
  /** SyntaxError, SemanticError, HTTP 404, Sin conexión, ... */
  readonly errorType: string
  readonly position: ErrorPosition | null

  constructor(init: {
    kind: ApiErrorKind
    status: number | null
    errorType: string
    message: string
    position?: ErrorPosition | null
  }) {
    super(init.message)
    this.kind = init.kind
    this.status = init.status
    this.errorType = init.errorType
    this.position = init.position ?? null
  }
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError'
}

/** Normaliza cualquier excepción a ApiError para mostrarla en la interfaz. */
export function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  const message = err instanceof Error ? err.message : String(err)
  return new ApiError({ kind: 'invalid', status: null, errorType: 'Error', message })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function parsePosition(value: unknown): ErrorPosition | null {
  if (!isRecord(value)) return null
  const { line, column, offset } = value
  if (typeof line !== 'number' || typeof column !== 'number' || typeof offset !== 'number') return null
  return { line, column, offset }
}

function plainText(text: string): string {
  const stripped = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
  return stripped.length > 300 ? `${stripped.slice(0, 300)}…` : stripped
}

function errorFromResponse(status: number, body: unknown, text: string): ApiError {
  const detail = isRecord(body) ? body.detail : undefined

  if (isRecord(detail) && typeof detail.message === 'string') {
    return new ApiError({
      kind: 'http',
      status,
      errorType: typeof detail.error === 'string' ? detail.error : `HTTP ${status}`,
      message: detail.message,
      position: parsePosition(detail.position),
    })
  }
  if (typeof detail === 'string') {
    return new ApiError({ kind: 'http', status, errorType: `HTTP ${status}`, message: detail })
  }
  if (Array.isArray(detail)) {
    // Errores de validación de FastAPI (422)
    const message = detail
      .map((d) => (isRecord(d) ? [Array.isArray(d.loc) ? d.loc.join('.') : '', String(d.msg ?? '')].filter(Boolean).join(': ') : String(d)))
      .join(' · ')
    return new ApiError({ kind: 'http', status, errorType: 'ValidationError', message: message || 'Petición inválida' })
  }
  // 502/503/504 (nginx) o 500 sin cuerpo (proxy de Vite): el backend no está disponible
  if (status === 502 || status === 503 || status === 504 || (status === 500 && text.trim() === '')) {
    return new ApiError({
      kind: 'network',
      status,
      errorType: 'Sin conexión',
      message: `El backend no responde (HTTP ${status}). Verifica que esté en ejecución.`,
    })
  }
  return new ApiError({ kind: 'http', status, errorType: `HTTP ${status}`, message: plainText(text) || 'Error del servidor' })
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  // Con FormData el navegador pone el Content-Type (multipart con su boundary)
  if (typeof init.body === 'string') headers['Content-Type'] = 'application/json'

  let res: Response
  let text: string
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers })
    text = await res.text()
  } catch (err) {
    if (isAbortError(err)) throw err
    throw new ApiError({
      kind: 'network',
      status: null,
      errorType: 'Sin conexión',
      message: 'No se pudo conectar con el backend de MiniDB. Verifica que esté en ejecución.',
    })
  }

  let body: unknown = undefined
  if (text !== '') {
    try {
      body = JSON.parse(text)
    } catch {
      body = undefined
    }
  }
  if (!res.ok) throw errorFromResponse(res.status, body, text)
  if (body === undefined) {
    throw new ApiError({
      kind: 'invalid',
      status: res.status,
      errorType: 'Respuesta inválida',
      message: 'El backend respondió con un contenido que no es JSON.',
    })
  }
  return body as T
}

// ---------------------------------------------------------------------------
// Normalización defensiva (el backend se desarrolla en paralelo)
// ---------------------------------------------------------------------------

const ZERO_METRICS: Metrics = { disk_reads: 0, disk_writes: 0, parse_ms: 0, exec_ms: 0, total_ms: 0 }

type Loose<T> = { [K in keyof T]?: T[K] | null }

function normalizeIo(raw: Loose<IoBreakdown> | null | undefined): IoBreakdown | undefined {
  if (!raw) return undefined
  return {
    index_reads: raw.index_reads ?? 0,
    index_writes: raw.index_writes ?? 0,
    table_reads: raw.table_reads ?? 0,
    table_writes: raw.table_writes ?? 0,
    other_reads: raw.other_reads ?? 0,
    other_writes: raw.other_writes ?? 0,
    files: raw.files ?? [],
  }
}

function normalizeMetrics(raw: Loose<Metrics> | null | undefined): Metrics {
  const io = normalizeIo(raw?.io)
  return {
    disk_reads: raw?.disk_reads ?? 0,
    disk_writes: raw?.disk_writes ?? 0,
    parse_ms: raw?.parse_ms ?? 0,
    exec_ms: raw?.exec_ms ?? 0,
    total_ms: raw?.total_ms ?? 0,
    ...(io ? { io } : {}),
  }
}

function normalizePlan(raw: Loose<Plan> | null | undefined): Plan | null {
  if (!raw || !raw.type) return null
  return {
    type: raw.type,
    table: raw.table ?? '',
    structure: raw.structure ?? '',
    index: raw.index ?? null,
    column: raw.column ?? null,
    predicate: raw.predicate ?? null,
    filter: raw.filter ?? null,
    estimated_io: raw.estimated_io ?? null,
    detail: raw.detail ?? null,
    planner: raw.planner ?? null,
    candidates: raw.candidates ?? [],
    strategy: raw.strategy ?? null,
    order_by: raw.order_by ?? null,
    limit: raw.limit ?? null,
    warning: raw.warning ?? null,
    spatial: raw.spatial ?? null,
    text: raw.text ?? null,
    vector: raw.vector ?? null,
    prefilter_rids: raw.prefilter_rids ?? null,
  }
}

function normalizeQuery(raw: Loose<QueryResponse>): QueryResponse {
  const rows = raw.rows ?? []
  return {
    statement: raw.statement ?? '',
    columns: raw.columns ?? [],
    rows,
    total_rows: raw.total_rows ?? rows.length,
    page: raw.page ?? 1,
    page_size: raw.page_size ?? Math.max(rows.length, 1),
    message: raw.message ?? '',
    plan: normalizePlan(raw.plan),
    metrics: normalizeMetrics(raw.metrics),
    statements: (raw.statements ?? []).map((s) => ({ ...s, metrics: normalizeMetrics(s.metrics) })),
  }
}

function normalizeTable(raw: Loose<TableInfo>): TableInfo {
  return {
    name: raw.name ?? '',
    organization: raw.organization ?? 'HEAP',
    page_size: raw.page_size ?? 0,
    primary_key: raw.primary_key ?? null,
    columns: raw.columns ?? [],
    record_size: raw.record_size ?? 0,
    records_per_page: raw.records_per_page ?? 0,
    record_count: raw.record_count ?? 0,
    page_count: raw.page_count ?? 0,
    overflow_page_count: raw.overflow_page_count ?? null,
    indexes: raw.indexes ?? [],
  }
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

const tablePath = (name: string) => `/api/tables/${encodeURIComponent(name)}`

export async function executeQuery(req: QueryRequest, signal?: AbortSignal): Promise<QueryResponse> {
  const raw = await request<Loose<QueryResponse>>('/api/query', {
    method: 'POST',
    body: JSON.stringify(req),
    signal,
  })
  return normalizeQuery(raw)
}

export async function fetchTables(signal?: AbortSignal): Promise<TableInfo[]> {
  const raw = await request<{ tables?: Loose<TableInfo>[] | null }>('/api/tables', { signal })
  return (raw.tables ?? []).map(normalizeTable)
}

export async function reorganizeTable(table: string): Promise<ReorganizeResponse> {
  const raw = await request<ReorganizeResponse>('/api/tables/reorganize', {
    method: 'POST',
    body: JSON.stringify({ table }),
  })
  return { ...raw, metrics: normalizeMetrics(raw.metrics) }
}

export async function fetchTableFiles(table: string, signal?: AbortSignal): Promise<TableFile[]> {
  const raw = await request<{ files?: TableFile[] | null }>(`${tablePath(table)}/files`, { signal })
  return raw.files ?? []
}

export async function fetchPage(table: string, pageId: number, file: string, signal?: AbortSignal): Promise<PageDump> {
  const query = new URLSearchParams({ file })
  return request<PageDump>(`${tablePath(table)}/pages/${pageId}?${query.toString()}`, { signal })
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthInfo> {
  return request<HealthInfo>('/api/health', { signal })
}

// --- Aplicación: recomendador de productos ---

/** URL de la imagen de un producto (GET /api/media/<id>.jpg), con la misma base que el resto de la API. */
export function mediaUrl(filename: string): string {
  return `${BASE}/api/media/${encodeURIComponent(filename)}`
}

/** Nombre de archivo que el backend sirve como imagen de producto: "<id>.jpg". */
export function isMediaFile(value: unknown): value is string {
  return typeof value === 'string' && /^\d+\.jpg$/.test(value)
}

export async function fetchAppStatus(signal?: AbortSignal): Promise<AppStatus> {
  const raw = await request<Loose<AppStatus>>('/api/app/status', { signal })
  return {
    table: raw.table ?? 'productos',
    loaded: raw.loaded ?? false,
    models: raw.models ?? false,
    media: raw.media ?? false,
    info: raw.info ?? null,
    methods: raw.methods ?? {},
  }
}

export async function fetchAppSamples(n: number, seed: number, signal?: AbortSignal): Promise<SampleProduct[]> {
  const query = new URLSearchParams({ n: String(n), seed: String(seed) })
  const raw = await request<{ products?: SampleProduct[] | null }>(`/api/app/samples?${query.toString()}`, { signal })
  return raw.products ?? []
}

export async function recommend(params: RecommendParams, signal?: AbortSignal): Promise<RecommendResponse> {
  const form = new FormData()
  const put = (name: string, value: string | number | null | undefined) => {
    if (value !== null && value !== undefined && value !== '') form.append(name, String(value))
  }
  if (params.image) form.append('image', params.image, params.image.name)
  put('product_id', params.product_id)
  put('text', params.text?.trim())
  put('price_min', params.price_min)
  put('price_max', params.price_max)
  put('lon', params.lon)
  put('lat', params.lat)
  put('radius_km', params.radius_km)
  put('category', params.category)
  put('k', params.k)
  put('probes', params.probes)
  put('strategy', params.strategy)
  if (params.methods && params.methods.length > 0) form.append('methods', params.methods.join(','))

  const raw = await request<Loose<RecommendResponse>>('/api/app/recommend', { method: 'POST', body: form, signal })
  if (!raw.query) {
    throw new ApiError({
      kind: 'invalid',
      status: null,
      errorType: 'Respuesta inválida',
      message: 'El backend respondió sin los datos de la consulta.',
    })
  }
  return {
    query: raw.query,
    features: raw.features ?? null,
    results: (raw.results ?? []).map((r: Loose<RecommendResult>) => {
      const rows = r.rows ?? []
      return {
        method: r.method ?? '',
        label: r.label ?? r.method ?? '',
        sql: r.sql ?? '',
        score_column: r.score_column ?? '',
        columns: r.columns ?? [],
        rows,
        total_rows: r.total_rows ?? rows.length,
        plan: normalizePlan(r.plan),
        metrics: r.metrics ? normalizeMetrics(r.metrics) : null,
        error: r.error ?? null,
      }
    }),
  }
}

export { ZERO_METRICS }
