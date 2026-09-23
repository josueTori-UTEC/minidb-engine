import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { SQL_EXAMPLES } from '../lib/examples'
import { RUN_SHORTCUT } from '../lib/platform'
import { Icon } from './Icon'

interface ExamplesMenuProps {
  onPick(sql: string): void
}

export function ExamplesMenu({ onPick }: ExamplesMenuProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuId = useId()

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) setOpen(false)
    }
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false)
        buttonRef.current?.focus()
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    rootRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  // Navegación con flechas entre ítems del menú
  const onMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End']
    if (!keys.includes(event.key)) return
    event.preventDefault()
    const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
    const current = items.findIndex((item) => item === document.activeElement)
    const last = items.length - 1
    const next =
      event.key === 'Home' ? 0
      : event.key === 'End' ? last
      : event.key === 'ArrowDown' ? (current >= last ? 0 : current + 1)
      : current <= 0 ? last : current - 1
    items[next]?.focus()
  }

  return (
    <div className="menu-root" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="btn btn-secondary"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="book" />
        Ejemplos
        <Icon name="chevronDown" size={14} className="btn-caret" />
      </button>
      {open && (
        <div className="menu" role="menu" id={menuId} aria-label="Ejemplos de SQL" onKeyDown={onMenuKeyDown}>
          {SQL_EXAMPLES.map((group) => (
            <div className="menu-group" role="group" aria-label={group.title} key={group.title}>
              <div className="menu-group-title" aria-hidden="true">
                {group.title}
              </div>
              {group.items.map((item) => (
                <button
                  key={item.sql}
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  onClick={() => {
                    setOpen(false)
                    onPick(item.sql)
                  }}
                >
                  <span className="menu-item-label">{item.label}</span>
                  <code className="menu-item-sql">{item.sql}</code>
                </button>
              ))}
            </div>
          ))}
          <p className="menu-footer">
            Se agrega al final del editor y queda seleccionado: <kbd>{RUN_SHORTCUT}</kbd> ejecuta solo ese fragmento.
          </p>
        </div>
      )}
    </div>
  )
}
