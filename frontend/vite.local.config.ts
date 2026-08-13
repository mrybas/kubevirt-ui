import base from './vite.config';
import { defineConfig, mergeConfig } from 'vite';

export default mergeConfig(base, defineConfig({
  server: {
    port: 5199,
    proxy: {
      '/api': { target: 'http://127.0.0.1:18000', changeOrigin: true, ws: true },
      '/health': { target: 'http://127.0.0.1:18000', changeOrigin: true },
    },
  },
}));
