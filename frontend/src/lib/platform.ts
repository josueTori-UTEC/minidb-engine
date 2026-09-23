const platform = typeof navigator === 'undefined' ? '' : navigator.platform || navigator.userAgent

export const IS_MAC = /Mac|iPhone|iPad|iPod/.test(platform)

/** Etiqueta del atajo de ejecución según el sistema operativo. */
export const RUN_SHORTCUT = IS_MAC ? '⌘ Enter' : 'Ctrl+Enter'
