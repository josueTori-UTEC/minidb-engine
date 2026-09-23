import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.tsx'
import { STORAGE_KEYS, readString } from './lib/storage'
import './styles.css'

// Se aplica el tema guardado antes del primer render para evitar un parpadeo
const savedTheme = readString(STORAGE_KEYS.theme)
if (savedTheme === 'light' || savedTheme === 'dark') document.documentElement.dataset.theme = savedTheme

const container = document.getElementById('root')
if (!container) throw new Error('No se encontró el elemento #root')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
