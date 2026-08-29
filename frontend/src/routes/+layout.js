// Spec §1: a static build served by the backend. No SSR — the app talks to /api with a
// session cookie, and prerendering an authenticated shell would only produce a flash of the
// wrong state.
export const ssr = false;
export const prerender = false;
