import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    // Spec §1: the PWA is a static build served by the backend. `fallback` makes it a true
    // SPA so deep links (/library/123) resolve client-side without a node server.
    adapter: adapter({ fallback: 'index.html', strict: false }),
    alias: { $lib: 'src/lib' }
  }
};

export default config;
