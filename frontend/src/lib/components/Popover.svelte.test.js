/**
 * @vitest-environment jsdom
 */

import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ leave: [] }));
vi.mock('$app/navigation', () => ({
  pushState: vi.fn(),
  replaceState: vi.fn(),
  beforeNavigate: (fn) => nav.leave.push(fn)
}));

import { pushState, replaceState } from '$app/navigation';
import Popover from './Popover.svelte';

const body = createRawSnippet(() => ({ render: () => '<div><input aria-label="Inside" /></div>' }));

let target;
let anchor;
let app;
const state = { open: true };

beforeEach(() => {
  nav.leave = [];
  target = document.createElement('div');
  anchor = document.createElement('button');
  anchor.textContent = 'People';
  document.body.append(anchor, target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
  anchor.remove();
});

function render(rect = { top: 100, bottom: 140, left: 300, right: 500 }, more = {}) {
  anchor.getBoundingClientRect = () => /** @type {DOMRect} */ ({ ...rect, width: 200, height: 40, x: rect.left, y: rect.top });
  const props = $state({ open: true, anchor, label: 'People', onClose: vi.fn(() => (props.open = false)), children: body, ...more });
  app = mount(Popover, { target, props });
  flushSync();
  return props;
}

const panel = () => target.querySelector('.popover');
// jsdom ships no PointerEvent, and the action reads none of it.
const tap = (/** @type {Element} */ el) =>
  el.dispatchEvent(new Event('pointerdown', { bubbles: true, cancelable: true }));

describe('a popover', () => {
  it('sits under its anchor at its width, and pushes no history entry', () => {
    const length = history.length;
    render();
    expect(panel().getAttribute('role')).toBe('dialog');
    expect(panel().getAttribute('aria-modal')).toBeNull();
    expect(panel().style.top).toBe('148px');
    expect(panel().style.left).toBe('300px');
    expect(panel().style.width).toBe('480px');
    expect(pushState).not.toHaveBeenCalled();
    expect(replaceState).not.toHaveBeenCalled();
    expect(history.length).toBe(length);
  });

  it("takes its anchor's width when given none", () => {
    render(undefined, { width: null });
    expect(panel().style.width).toBe('200px');
  });

  it('opens above its anchor when there is no room below', () => {
    render({ top: 700, bottom: 740, left: 900, right: 1000 });
    expect(panel().style.top).toBe('');
    expect(panel().style.bottom).toBe(`${window.innerHeight - 700 + 8}px`);
    expect(panel().style.left).toBe(`${window.innerWidth - 16 - 480}px`);
    expect(panel().style.maxHeight).toBe(`${700 - 8 - 16}px`);
  });

  it('closes on a press outside, but not on one inside or on its anchor', () => {
    const props = render();
    tap(panel().querySelector('input'));
    tap(anchor);
    expect(props.onClose).not.toHaveBeenCalled();
    tap(document.body);
    expect(props.onClose).toHaveBeenCalledOnce();
    flushSync();
    expect(panel()).toBeNull();
  });

  it('closes on Escape and gives focus back to what opened it', () => {
    anchor.focus();
    const props = render();
    panel().querySelector('input').focus();
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(props.onClose).toHaveBeenCalledOnce();
    flushSync();
    expect(panel()).toBeNull();
    expect(document.activeElement).toBe(anchor);
  });

  it('closes when the page navigates', () => {
    const props = render();
    nav.leave.forEach((fn) => fn());
    expect(props.onClose).toHaveBeenCalledOnce();
  });
});
