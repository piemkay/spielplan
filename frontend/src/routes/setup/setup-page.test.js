/**
 * @vitest-environment jsdom
 *
 * Not `+page.test.js`: SvelteKit reserves the `+` prefix inside `src/routes`.
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
  // An admin exists and nobody is signed in, so the page opens past step one.
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
