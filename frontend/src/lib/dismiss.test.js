/**
 * @vitest-environment jsdom
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { dismiss } from './dismiss.js';

function guarded() {
  const node = document.createElement('div');
  const inner = document.createElement('button');
  node.appendChild(inner);
  document.body.appendChild(node);
  const outside = document.createElement('div');
  document.body.appendChild(outside);
  return { node, inner, outside };
}

// On a real target and bubbling: the action reads `composedPath()`. jsdom has no PointerEvent.
const tap = (/** @type {Element} */ target) =>
  target.dispatchEvent(new Event('pointerdown', { bubbles: true, cancelable: true }));

const press = (/** @type {string} */ key, /** @type {Element} */ target = document.body) =>
  target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));

afterEach(() => {
  document.body.innerHTML = '';
});

describe('the dismiss action', () => {
  it('closes when the pointer lands outside the node', () => {
    const { node, outside } = guarded();
    const close = vi.fn();
    const handle = dismiss(node, close);
    try {
      tap(outside);
      expect(close, 'a tap on the backdrop did not dismiss').toHaveBeenCalledTimes(1);
    } finally {
      handle.destroy();
    }
  });

  it('does not close when the pointer lands inside it', () => {
    const { node, inner } = guarded();
    const close = vi.fn();
    const handle = dismiss(node, close);
    try {
      tap(node);
      tap(inner);
      expect(close, 'a tap on the overlay dismissed it').not.toHaveBeenCalled();
    } finally {
      handle.destroy();
    }
  });

  it('closes on Escape, from anywhere on the page', () => {
    const { node, outside } = guarded();
    const close = vi.fn();
    const handle = dismiss(node, close);
    try {
      press('Escape', outside);
      press('Escape', node);
      expect(close, 'Escape did not dismiss').toHaveBeenCalledTimes(2);
    } finally {
      handle.destroy();
    }
  });

  it('dismisses every mounted overlay on one Escape, which is the rule and not an accident', () => {
    // Two overlays coexist on one path (a title panel, then `m` opens the rail over it); both answer.
    const first = guarded();
    const second = guarded();
    const closeFirst = vi.fn();
    const closeSecond = vi.fn();
    const a = dismiss(first.node, closeFirst);
    const b = dismiss(second.node, closeSecond);
    try {
      press('Escape');
      expect(closeFirst, 'the overlay underneath did not answer').toHaveBeenCalledTimes(1);
      expect(closeSecond, 'the overlay on top did not answer').toHaveBeenCalledTimes(1);

      // A tap inside the top overlay is outside the one underneath, so that one closes.
      closeFirst.mockClear();
      closeSecond.mockClear();
      tap(second.inner);
      expect(closeSecond, 'a tap inside an overlay dismissed it').not.toHaveBeenCalled();
      expect(closeFirst, 'a tap outside an overlay did not dismiss it').toHaveBeenCalledTimes(1);
    } finally {
      a.destroy();
      b.destroy();
    }
  });

  it('leaves every other key alone', () => {
    // The shell's `m` shortcut opens the rail, so an any-key dismissal would fight it.
    const { node } = guarded();
    const close = vi.fn();
    const handle = dismiss(node, close);
    try {
      for (const key of ['m', 'Enter', 'Tab', 'Esc', ' ']) press(key);
      expect(close, 'a key that is not Escape dismissed').not.toHaveBeenCalled();
    } finally {
      handle.destroy();
    }
  });

  it('hands the event to the callback, so an opener tap is distinguishable', () => {
    // ModelRail's trigger sits outside the rail and toggles; without the event it would reopen.
    const { node, outside } = guarded();
    const close = vi.fn();
    const handle = dismiss(node, close);
    try {
      tap(outside);
      press('Escape');
      const [[pointer], [key]] = close.mock.calls;
      expect(pointer.type).toBe('pointerdown');
      expect(pointer.target).toBe(outside);
      expect(key.type).toBe('keydown');
    } finally {
      handle.destroy();
    }
  });

  it('takes a replaced callback, because two call sites pass a prop through', () => {
    const { node, outside } = guarded();
    const first = vi.fn();
    const second = vi.fn();
    const handle = dismiss(node, first);
    try {
      handle.update(second);
      tap(outside);
      expect(first, 'the replaced callback is still being called').not.toHaveBeenCalled();
      expect(second).toHaveBeenCalledTimes(1);
    } finally {
      handle.destroy();
    }
  });

  it('removes both listeners on destroy', () => {
    // By behaviour, not a `removeEventListener` spy: the capture flag has to match.
    const { node, outside } = guarded();
    const close = vi.fn();
    dismiss(node, close).destroy();

    tap(outside);
    press('Escape');
    expect(close, 'a destroyed action is still listening on document').not.toHaveBeenCalled();
  });
});
