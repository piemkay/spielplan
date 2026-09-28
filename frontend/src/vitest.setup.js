import { vi } from 'vitest';

// Outside a running SvelteKit app `$app/state` cannot load (its client imports the router the
// tests mock). A test that simulates shallow routing does it on a mocked `$app/stores` page, so
// `page.state` here reads that store, reactively.
vi.mock('$app/state', async () => {
  const { fromStore } = await import('svelte/store');
  let store = null;
  try {
    store = (await import('$app/stores')).page ?? null;
  } catch {
    store = null;
  }
  const current = store ? fromStore(store) : null;
  return {
    page: {
      get state() {
        return current?.current?.state ?? {};
      },
      get url() {
        return current?.current?.url ?? new URL('http://localhost/');
      }
    }
  };
});
