import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    // SPA fallback: deep links resolve client-side without a node server (spec §1).
    adapter: adapter({ fallback: 'index.html', strict: false }),
    // Notice a deploy before a stale chunk import fails mid-verdict, so `+layout.svelte`'s
    // `beforeNavigate` can reload at a route change instead.
    version: { pollInterval: 60000 },
    alias: { $lib: 'src/lib' }
  }
};

export default config;
