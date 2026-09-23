/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** URL base del backend (vacía = misma origin, vía proxy de Vite o nginx). */
  readonly VITE_API_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
