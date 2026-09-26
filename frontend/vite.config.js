import { sveltekit } from '@sveltejs/kit/vite';

// Cast: jsconfig has no node types, and adding @types/node would change package-lock.json.
const { API_ORIGIN, VITEST } = /** @type {any} */ (globalThis).process?.env ?? {};

export default {
  plugins: [sveltekit()],
  // Vitest would otherwise resolve svelte's SSR build, where mount() throws; the prerender at
  // build time still needs the server condition.
  resolve: VITEST ? { conditions: ['browser'] } : undefined,
  server: {
    proxy: {
      '/api': { target: API_ORIGIN ?? 'http://127.0.0.1:8080', changeOrigin: true }
    }
  }
};
