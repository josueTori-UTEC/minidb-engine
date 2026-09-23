import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// En desarrollo las peticiones relativas a /api se reenvían al backend de FastAPI.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    rolldownOptions: {
      output: {
        // Dependencias en chunks separados para que el navegador las cachee entre despliegues
        codeSplitting: {
          groups: [
            { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 30 },
            {
              name: 'editor',
              test: /node_modules[\\/](@codemirror|@lezer|@uiw|@marijn|codemirror|crelt|style-mod|w3c-keyname)[\\/]/,
              priority: 20,
            },
            { name: 'vendor', test: /node_modules[\\/]/, priority: 10 },
          ],
        },
      },
    },
  },
})
