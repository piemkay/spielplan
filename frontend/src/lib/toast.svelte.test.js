/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';

import Toast from './components/Toast.svelte';
import { hideToast, showToast } from './toast.svelte.js';

let app;
let target;

afterEach(() => {
  hideToast();
  if (app) unmount(app);
  app = null;
  target?.remove();
});

it('speaks from a status mounted before the message, and the words leave with the box', () => {
  target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(Toast, { target });
  flushSync();
  const status = target.querySelector('[role="status"]');
  const box = target.querySelector('[data-testid="toast"]');
  expect(status.textContent).toBe('');

  const run = vi.fn();
  showToast('Heat moved to A', { label: 'Undo', run });
  flushSync();
  expect(target.querySelector('[role="status"]'), 'the status was replaced, not filled').toBe(status);
  expect(status.textContent).toBe('Heat moved to A');
  expect(box.classList.contains('open')).toBe(true);

  showToast('Drive moved to B');
  flushSync();
  expect(status.textContent).toBe('Drive moved to B');
  expect(box.querySelector('button'), 'an Undo carried over to a toast that has none').toBeNull();

  hideToast();
  flushSync();
  expect(status.textContent).toBe('');
  expect(box.classList.contains('open')).toBe(false);
  expect(box.textContent, 'the words vanished before the box left').toContain('Drive moved to B');
});

it('runs its action once and closes', () => {
  target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(Toast, { target });
  const run = vi.fn();
  showToast('Heat moved to A', { label: 'Undo', run });
  flushSync();

  /** @type {HTMLButtonElement} */ (target.querySelector('[data-testid="toast"] button')).click();
  flushSync();
  expect(run).toHaveBeenCalledTimes(1);
  expect(target.querySelector('[data-testid="toast"]').classList.contains('open')).toBe(false);
});
