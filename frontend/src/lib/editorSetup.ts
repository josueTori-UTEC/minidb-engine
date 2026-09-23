import { SQLDialect, sql, type SQLNamespace } from '@codemirror/lang-sql'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags as t } from '@lezer/highlight'
import { EditorView, type Extension } from '@uiw/react-codemirror'
import type { TableInfo } from '../api'
import { columnTypeLabel } from './format'

// Solo las palabras clave de la gramática de MiniDB: lo que no se resalta, el parser no lo acepta.
const MINIDB_DIALECT = SQLDialect.define({
  keywords:
    'select from where and between insert into values delete create table index on using primary key ' +
    'drop copy explain heap sequential btree hash',
  types: 'int float char varchar',
  builtin: '',
  backslashEscapes: false,
  doubleQuotedStrings: false,
  hashComments: false,
  slashComments: false,
})

const highlightStyle = HighlightStyle.define([
  { tag: t.keyword, color: 'var(--syn-keyword)', fontWeight: '600' },
  { tag: t.typeName, color: 'var(--syn-type)' },
  { tag: [t.string, t.special(t.string)], color: 'var(--syn-string)' },
  { tag: [t.number, t.bool, t.null], color: 'var(--syn-number)' },
  { tag: [t.lineComment, t.blockComment], color: 'var(--syn-comment)', fontStyle: 'italic' },
  { tag: t.operator, color: 'var(--syn-operator)' },
  { tag: [t.punctuation, t.paren], color: 'var(--syn-punct)' },
])

export const editorTheme: Extension = EditorView.theme({
  '&': {
    height: '100%',
    fontSize: 'var(--fs-code)',
    color: 'var(--text)',
    backgroundColor: 'var(--editor-bg)',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-sql-error': {
    textDecoration: 'underline wavy var(--danger)',
    textUnderlineOffset: '3px',
    backgroundColor: 'var(--danger-soft)',
    borderRadius: '2px',
  },
  '.cm-scroller': { fontFamily: 'var(--font-mono)', lineHeight: '1.65' },
  '.cm-content': { padding: '8px 0', caretColor: 'var(--accent)' },
  '.cm-line': { padding: '0 12px 0 8px' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--accent)', borderLeftWidth: '2px' },
  '&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
    { backgroundColor: 'var(--editor-selection)' },
  '.cm-gutters': {
    backgroundColor: 'var(--editor-gutter)',
    color: 'var(--text-3)',
    borderRight: '1px solid var(--border)',
  },
  '.cm-lineNumbers .cm-gutterElement': { padding: '0 10px 0 12px', minWidth: '32px' },
  '.cm-activeLine': { backgroundColor: 'var(--editor-active-line)' },
  '.cm-activeLineGutter': { backgroundColor: 'var(--editor-active-line)', color: 'var(--text-2)' },
  '.cm-placeholder': { color: 'var(--text-3)', fontStyle: 'italic' },
  '&.cm-focused .cm-matchingBracket': { backgroundColor: 'var(--editor-bracket)', outline: 'none' },
  '.cm-tooltip': {
    backgroundColor: 'var(--panel)',
    color: 'var(--text)',
    border: '1px solid var(--border-strong)',
    borderRadius: '6px',
    boxShadow: 'var(--shadow-md)',
    overflow: 'hidden',
  },
  '.cm-tooltip-autocomplete > ul': { fontFamily: 'var(--font-mono)', fontSize: '12px' },
  '.cm-tooltip-autocomplete > ul > li': { padding: '3px 8px' },
  '.cm-tooltip-autocomplete > ul > li[aria-selected]': {
    backgroundColor: 'var(--accent-soft)',
    color: 'var(--text)',
  },
  '.cm-completionDetail': { color: 'var(--text-3)', fontStyle: 'normal', marginLeft: '12px' },
  '.cm-panels': { backgroundColor: 'var(--panel-2)', color: 'var(--text)' },
  '.cm-panels.cm-panels-bottom': { borderTop: '1px solid var(--border)' },
  '.cm-searchMatch': { backgroundColor: 'var(--editor-search)' },
})

/** Extensiones de lenguaje: dialecto de MiniDB + autocompletado de tablas y columnas del catálogo. */
export function sqlLanguage(tables: readonly TableInfo[]): Extension[] {
  const schema: SQLNamespace = Object.fromEntries(
    tables.map((table) => [
      table.name,
      {
        self: { label: table.name, type: 'type', detail: table.organization },
        children: table.columns.map((col) => ({
          label: col.name,
          type: 'property',
          detail: columnTypeLabel(col) + (col.primary_key ? ' · PK' : ''),
        })),
      },
    ]),
  )
  const defaultTable = tables.length === 1 ? tables[0]?.name : undefined
  return [
    sql({ dialect: MINIDB_DIALECT, schema, defaultTable, upperCaseKeywords: true }),
    syntaxHighlighting(highlightStyle),
  ]
}
