import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    // FastAPI serves this directory directly (see app/main.py) - built
    // assets end up alongside the backend rather than needing a separate
    // static host.
    outDir: 'dist',
  },
  server: {
    // During `npm run dev`, proxy API calls to the FastAPI backend
    // (run separately via `python run.py`) so the frontend can be
    // developed with hot-reload while still hitting real data.
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/_next': 'http://127.0.0.1:8000',
    },
  },
})
