import { API_TARGET, type HealthInfo } from '../api'
import { Icon, type IconName } from './Icon'

export type HealthState =
  | { status: 'checking' }
  | { status: 'online'; info: HealthInfo }
  | { status: 'offline'; message: string }

export type ThemePreference = 'system' | 'light' | 'dark'

const THEME_OPTIONS: { value: ThemePreference; icon: IconName; label: string }[] = [
  { value: 'system', icon: 'monitor', label: 'Tema del sistema' },
  { value: 'light', icon: 'sun', label: 'Tema claro' },
  { value: 'dark', icon: 'moon', label: 'Tema oscuro' },
]

interface AppHeaderProps {
  health: HealthState
  theme: ThemePreference
  onTheme(theme: ThemePreference): void
  onRecheck(): void
}

export function AppHeader({ health, theme, onTheme, onRecheck }: AppHeaderProps) {
  const label =
    health.status === 'online' ? 'Backend en línea' : health.status === 'offline' ? 'Sin conexión' : 'Conectando…'
  const details =
    health.status === 'online'
      ? [`API: ${API_TARGET}`, health.info.data_dir && `Datos: ${health.info.data_dir}`, health.info.tables != null && `Tablas: ${health.info.tables}`]
      : health.status === 'offline'
        ? [`API: ${API_TARGET}`, health.message]
        : [`API: ${API_TARGET}`]

  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-logo" aria-hidden="true">
          <Icon name="database" size={17} />
        </span>
        <h1 className="brand-name">MiniDB</h1>
        <span className="brand-sub">Motor relacional en disco · CS2042 Base de Datos II</span>
      </div>

      <div className="header-right">
        <button
          type="button"
          className={`status-pill is-${health.status}`}
          onClick={onRecheck}
          title={`${details.filter(Boolean).join('\n')}\n(clic para volver a comprobar)`}
          aria-live="polite"
        >
          <span className="status-dot" aria-hidden="true" />
          <span>{label}</span>
          {health.status === 'online' && health.info.data_dir && <span className="status-meta">{health.info.data_dir}</span>}
        </button>

        <div className="segmented theme-toggle" role="group" aria-label="Tema de color">
          {THEME_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              aria-pressed={theme === option.value}
              onClick={() => onTheme(option.value)}
              title={option.label}
            >
              <Icon name={option.icon} size={14} />
              <span className="sr-only">{option.label}</span>
            </button>
          ))}
        </div>
      </div>
    </header>
  )
}
