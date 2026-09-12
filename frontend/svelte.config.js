import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    // Spec §1: the PWA is a static build served by the backend. `fallback` makes it a true
    // SPA so deep links (/library/123) resolve client-side without a node server.
    adapter: adapter({ fallback: 'index.html', strict: false }),
    // M4.15 finding 23: a deploy used to reload every open phone at its next tap. The service
    // worker calls `skipWaiting()` and clears the other caches on activate — correct for a
    // single-host appliance — so a tab open across a deploy has lost the chunks it is about to
    // import, and SvelteKit's fallback is a native navigation. That fallback is right; with no
    // `pollInterval` the version check simply fired on the import failure itself, which is to
    // say mid-round or mid-block, on the tap meant to record a verdict. A minute's polling puts
    // the flag up first, so `+layout.svelte`'s `beforeNavigate` can take the deploy at a route
    // change the person chose. Sixty seconds because it is one conditional request to the same
    // origin, and the household is on the LAN.
    version: { pollInterval: 60000 },
    alias: { $lib: 'src/lib' }
  }
};

export default config;
