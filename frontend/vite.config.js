import { sveltekit } from '@sveltejs/kit/vite';

// Read once: `jsconfig.json` has no node types, so every `process` in this file is an error
// `npm run check` reports, and the second one would have been a new one on a baseline the
// milestone measures against.
const { API_ORIGIN, VITEST } = process.env;

export default {
  plugins: [sveltekit()],
  // Vitest resolves through Vite, and by default it takes the server condition: `svelte` comes
  // back as its SSR build, `mount()` throws `lifecycle_function_unavailable`, and a component
  // test cannot exist at all — which is why §6.7's rail had none. Guarded on VITEST because the
  // prerender adapter-static runs at build time needs the server build it excludes.
  // [M4.9 finding 27]
  resolve: VITEST ? { conditions: ['browser'] } : undefined,
  server: {
    // Dev only: the SvelteKit dev server proxies the API to the backend so the browser
    // sees one origin, exactly as it will in production where the backend serves both.
    proxy: {
      '/api': { target: API_ORIGIN ?? 'http://127.0.0.1:8080', changeOrigin: true }
    }
  }
};
