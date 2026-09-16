import { sveltekit } from '@sveltejs/kit/vite';

// Read once, and through a cast: `jsconfig.json` has no node types, so a bare `process` here
// is one of the errors `npm --prefix frontend run check` reports — and that command is now a
// CI job step rather than a documented command nobody runs (decision 273), so an error left
// standing here costs the whole frontend gate. The cast is the idiom `push.js:106` already
// uses for a global lib.dom does not model. `@types/node` is deliberately NOT the fix: a
// devDependency added for one name changes `package-lock.json`, which both `npm ci` in CI and
// the frontend image build resolve against. Nothing about the value read changes — this file
// is only ever loaded by node. [M4.15 finding 26]
const { API_ORIGIN, VITEST } = /** @type {any} */ (globalThis).process?.env ?? {};

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
