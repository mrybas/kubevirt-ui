import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    target: 'esnext',
  },
  optimizeDeps: {
    esbuildOptions: {
      target: 'esnext',
    },
  },
  server: {
    port: 3000,
    allowedHosts: ['frontend', 'localhost'],
    proxy: {
      // `backend:8000` is the compose service. Running vite outside compose
      // (the frontend image does not build on arm64 — rollup has no musl
      // binary for it) needs the published port instead, so the target is
      // overridable without editing this file:
      //   VITE_BACKEND_TARGET=http://localhost:8888 npm run dev
      '/api': {
        target: process.env.VITE_BACKEND_TARGET || 'http://backend:8000',
        changeOrigin: true,
        ws: true, // Enable WebSocket proxy
      },
      '/health': {
        target: process.env.VITE_BACKEND_TARGET || 'http://backend:8000',
        changeOrigin: true,
      },
    },
  },
});
