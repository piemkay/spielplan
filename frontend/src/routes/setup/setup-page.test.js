/**
 * @vitest-environment jsdom
 *
 * The wizard's bundle step when a restart is owed. Spec v2.1 §3.1, §10; decision 497.
 *
 * The first household's wizard ended on the importer's "Restart backend and worker" with no
 * command in it, over a header saying "no bundle imported". The backend now loads an imported
 * bundle by itself, so the step says so; the one state left that owes a restart - a bundle the
 * backend could not load - is named on this step with the command, for an operator who comes back
 * to it later rather than watching an import finish here.
 *
 * Named `setup-page.test.js` because SvelteKit reserves the `+` prefix inside `src/routes`.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

import { session } from '$lib/session.svelte.js';
import SetupPage from './+page.svelte';

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  // An admin exists, so the page skips its own bootstrap and opens past step one; nobody is
  // signed in on this device, so the importer asks the admin-only state route nothing.
  Object.assign(session, { user: null, setup: { required: false, note: 'ready' } });
});

afterEach(() => {
  target.remove();
  session.restartRequired = null;
});

async function settle() {
  for (let i = 0; i < 30; i++) await Promise.resolve();
  flushSync();
}

async function openBundleStep() {
  const app = mount(SetupPage, { target });
  await settle();
  target.querySelectorAll('[data-testid="setup-step"]')[2].click();
  await settle();
  return app;
}

describe("the wizard's bundle step", () => {
  it('names the restart and its command when the backend could not load an imported bundle', async () => {
    session.restartRequired = true;
    const app = await openBundleStep();
    try {
      const owed = target.querySelector('[data-restart-required]');
      expect(owed, 'the step said nothing about a restart that is owed').not.toBeNull();
      expect(owed.textContent).toContain('docker compose restart backend worker');
    } finally {
      unmount(app);
    }
  });

  it('says nothing about restarting when nothing is owed', async () => {
    session.restartRequired = false;
    const app = await openBundleStep();
    try {
      expect(target.querySelector('[data-restart-required]')).toBeNull();
      expect(target.textContent).toContain('nothing needs restarting');
    } finally {
      unmount(app);
    }
  });
});
