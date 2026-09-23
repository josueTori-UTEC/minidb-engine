import CodeMirror, {
  EditorSelection,
  EditorView,
  Prec,
  keymap,
  type BasicSetupOptions,
  type EditorState,
  type ViewUpdate,
} from '@uiw/react-codemirror'
import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from 'react'
import type { TableInfo } from '../api'
import { editorTheme, sqlLanguage } from '../lib/editorSetup'
import { fmtInt } from '../lib/format'
import { RUN_SHORTCUT } from '../lib/platform'
import { ExamplesMenu } from './ExamplesMenu'
import { Icon } from './Icon'

export interface RunTarget {
  sql: string
  /** Offset del texto enviado dentro del documento (inicio de la selección o 0). */
  base: number
  isSelection: boolean
}

export interface SqlEditorHandle {
  /** Agrega un fragmento al final del editor, lo selecciona y enfoca el editor. */
  insertSnippet(text: string): void
  /** Selecciona el token en base + offset, si el texto enviado sigue intacto en el editor. */
  revealError(sentSql: string, base: number, offset: number): void
  run(): void
}

interface SqlEditorProps {
  value: string
  onChange(value: string): void
  onRun(target: RunTarget): void
  running: boolean
  locked: boolean
  tables: readonly TableInfo[]
  ref?: Ref<SqlEditorHandle>
}

const BASIC_SETUP: BasicSetupOptions = {
  lineNumbers: true,
  highlightActiveLine: true,
  highlightActiveLineGutter: true,
  foldGutter: false,
  bracketMatching: true,
  closeBrackets: true,
  autocompletion: true,
  highlightSelectionMatches: false,
  allowMultipleSelections: false,
  rectangularSelection: false,
  crosshairCursor: false,
  lintKeymap: false,
  tabSize: 2,
}

/** Selección no vacía → solo la selección; si no, todo el contenido del editor. */
function runTargetFrom(state: EditorState): RunTarget | null {
  const { from, to } = state.selection.main
  if (from !== to) {
    const selected = state.sliceDoc(from, to)
    if (selected.trim() !== '') return { sql: selected, base: from, isSelection: true }
  }
  const all = state.doc.toString()
  return all.trim() === '' ? null : { sql: all, base: 0, isSelection: false }
}

interface CursorInfo {
  line: number
  column: number
  selected: number
  hasSelection: boolean
}

function cursorInfo(state: EditorState): CursorInfo {
  const { from, to, head } = state.selection.main
  const line = state.doc.lineAt(head)
  return {
    line: line.number,
    column: head - line.from + 1,
    selected: to - from,
    hasSelection: from !== to && state.sliceDoc(from, to).trim() !== '',
  }
}

export function SqlEditor({ value, onChange, onRun, running, locked, tables, ref }: SqlEditorProps) {
  const viewRef = useRef<EditorView | null>(null)
  const [cursor, setCursor] = useState<CursorInfo>({ line: 1, column: 1, selected: 0, hasSelection: false })
  const [notice, setNotice] = useState<string | null>(null)

  const run = useCallback(() => {
    const view = viewRef.current
    if (!view || locked) return
    const target = runTargetFrom(view.state)
    if (!target) {
      setNotice('El editor está vacío: escribe una sentencia SQL.')
      view.focus()
      return
    }
    setNotice(null)
    onRun(target)
  }, [locked, onRun])

  // El keymap de CodeMirror se crea una vez; siempre invoca la versión más reciente de run().
  const runRef = useRef(run)
  useEffect(() => {
    runRef.current = run
  }, [run])

  const extensions = useMemo(() => {
    const runFromKeyboard = () => {
      runRef.current()
      return true
    }
    return [
      ...sqlLanguage(tables),
      Prec.highest(
        keymap.of([
          { key: 'Mod-Enter', preventDefault: true, run: runFromKeyboard },
          { key: 'Ctrl-Enter', preventDefault: true, run: runFromKeyboard },
        ]),
      ),
      EditorView.contentAttributes.of({ 'aria-label': 'Editor SQL' }),
    ]
  }, [tables])

  const insertSnippet = useCallback((text: string) => {
    const view = viewRef.current
    if (!view) return
    const doc = view.state.doc.toString()
    const replaceAll = doc.trim() === ''
    const prefix = replaceAll || doc.endsWith('\n') ? '' : '\n'
    const from = replaceAll ? 0 : doc.length
    const start = from + prefix.length
    view.dispatch({
      changes: { from, to: doc.length, insert: prefix + text },
      selection: EditorSelection.single(start, start + text.length),
      scrollIntoView: true,
      userEvent: 'input.paste',
    })
    view.focus()
  }, [])

  useImperativeHandle(
    ref,
    () => ({
      insertSnippet,
      revealError(sentSql: string, base: number, offset: number) {
        const view = viewRef.current
        if (!view) return
        const doc = view.state.doc
        if (base + sentSql.length > doc.length || doc.sliceString(base, base + sentSql.length) !== sentSql) return
        const pos = Math.min(base + Math.max(offset, 0), doc.length)
        const ahead = doc.sliceString(pos, Math.min(pos + 80, doc.length))
        const word = /^[\p{L}\p{N}_]+/u.exec(ahead)?.[0].length ?? (ahead !== '' && !/^\s/.test(ahead) ? 1 : 0)
        view.dispatch({
          selection: EditorSelection.single(pos, pos + word),
          effects: EditorView.scrollIntoView(pos, { y: 'center' }),
        })
        view.focus()
      },
      run: () => runRef.current(),
    }),
    [insertSnippet],
  )

  const handleUpdate = useCallback((update: ViewUpdate) => {
    if (update.selectionSet || update.docChanged) setCursor(cursorInfo(update.state))
    if (update.docChanged) setNotice(null)
  }, [])

  const clear = () => {
    const view = viewRef.current
    if (!view) return
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: '' }, userEvent: 'delete' })
    view.focus()
  }

  const runLabel = cursor.hasSelection ? 'Ejecutar selección' : 'Ejecutar'

  return (
    <section className="panel panel-editor" aria-labelledby="editor-title">
      <header className="panel-header">
        <h2 className="panel-title" id="editor-title">
          <Icon name="code" />
          Editor SQL
        </h2>
        <div className="panel-actions">
          <ExamplesMenu onPick={insertSnippet} />
          <button type="button" className="btn btn-ghost btn-icon" onClick={clear} title="Vaciar el editor (se puede deshacer con Ctrl+Z)" aria-label="Vaciar el editor">
            <Icon name="eraser" />
          </button>
          <button
            type="button"
            className="btn btn-primary btn-run"
            onClick={run}
            disabled={locked}
            aria-keyshortcuts="Control+Enter Meta+Enter"
            title={`${runLabel} (${RUN_SHORTCUT})`}
          >
            {running ? <span className="spinner" aria-hidden="true" /> : <Icon name="play" size={14} />}
            {running ? 'Ejecutando…' : runLabel}
            <kbd className="kbd-inverse">{RUN_SHORTCUT}</kbd>
          </button>
        </div>
      </header>

      <div className="editor-host">
        <CodeMirror
          className="sql-editor"
          value={value}
          onChange={onChange}
          onUpdate={handleUpdate}
          onCreateEditor={(view) => {
            viewRef.current = view
            setCursor(cursorInfo(view.state))
          }}
          height="100%"
          theme={editorTheme}
          basicSetup={BASIC_SETUP}
          extensions={extensions}
          placeholder={`Escribe una sentencia SQL y presiona ${RUN_SHORTCUT} para ejecutarla…`}
        />
      </div>

      <footer className="statusbar" aria-live="polite">
        {notice ? (
          <span className="statusbar-notice">
            <Icon name="alert" size={13} />
            {notice}
          </span>
        ) : (
          <span>
            Ln {cursor.line}, Col {cursor.column}
            {cursor.selected > 0 && ` · ${fmtInt(cursor.selected)} seleccionados`}
          </span>
        )}
        <span className="statusbar-spacer" />
        <span className="statusbar-hint">
          {cursor.hasSelection ? 'Se ejecutará solo la selección' : 'Se ejecutará todo el editor'}
        </span>
        <span className="statusbar-sep" aria-hidden="true" />
        <span>MiniDB SQL</span>
      </footer>
    </section>
  )
}
