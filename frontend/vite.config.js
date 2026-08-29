import { sveltekit } from '@sveltejs/kit/vite';

export default {
  plugins: [sveltekit()],
  server: {
    // Dev only: the SvelteKit dev server proxies the API to the backend so the browser
    // sees one origin, exactly as it will in production where the backend serves both.
    proxy: {
      '/api': { target: process.env.API_ORIGIN ?? 'http://127.0.0.1:8080', changeOrigin: true }
    }
  }
};
