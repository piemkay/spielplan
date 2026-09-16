/**
 * @vitest-environment jsdom
 *
 * The one dismissal, exercised as the action rather than through the three surfaces that use it.
 * Spec v2.1 §6 preamble; proposals 127 and 131.
 *
 * HERE, AND NOT ONLY IN PLAYWRIGHT. `19-phone-shell.spec.js` asserts the household's version of
 * this — the account menu, the title panel and the model rail each close by an outside tap and by
 * Escape — and that is the claim the coverage row carries. What a browser cannot show cheaply is
 * the half that makes the rule safe to apply three times: that the listeners are on `document`,
 * that both of them come off again on destroy, and that the callback is handed the event so a
 * node whose opener lives outside it can tell an opener tap from every other outside tap. A leak
 * of the first kind is invisible until the fourth surface adopts the action, and by then it is
 * one stale handler per overlay that has ever been opened.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { dismiss } from './dismiss.js';

/** A node the action can guard, with one child, mounted where a real overlay would be. */
function guarded() {
  const node = document.createElement('div');
  const inner = document.createElement('button');
  node.appendChild(inner);
  document.body.appendChild(node);
  const outside = document.createElement('div');
  document.body.appendChild(outside);
  return { node, inner, outside };
}

/**
 * A pointerdown as an engine sends it: on a real target and bubbling, because the action reads
 * `composedPath()` and an event dispatched on `document` itself has a path of one.
 *
 * `Event` rather than `PointerEvent` — jsdom ships no `PointerEvent` constructor, and the action
 * touches nothing on the interface beyond `type`, `target` and `composedPath`.
 */
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
    // The half that makes `.wrap` rather than `.menu` the right node in `AccountChip`: the chip
    // button is inside the guarded node, so its own tap must reach `toggle()` untouched.
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
    // Two guarded nodes exist together on exactly one reachable path: with §6.7's "show the
    // model" on, a title panel opened from Home and then `m` - `+layout.svelte`'s shortcut is on
    // the window and skips only INPUT/TEXTAREA/SELECT/contentEditable - opens the rail over the
    // panel. Neither instance knows it is the topmost, so both answer. Held here because nothing
    // else in the suite mounts two: `ModelRail.svelte.test.js` and `TitleDetail.svelte.test.js`
    // each mount one, and `19-phone-shell` closes each overlay before opening the next, so a
    // stack added later would land with the whole suite green. [review cycle 3: M415-C3-COMP-04]
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

      // The pointerdown half of the same shape, and here the answer is not symmetrical: a tap
      // inside the top overlay IS outside the one underneath, so dismissing that one is correct.
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
    // Proposal 118 gives the model rail an `m` shortcut on the shell's own keydown handler, so a
    // dismissal that fired on any key would fight the toggle that opened it.
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
    // `ModelRail`'s trigger is in the shell header and outside the rail, and it toggles: without
    // the event the rail would close on its pointerdown and reopen on the click that follows,
    // which is a button that no longer closes.
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
    // Both, and by behaviour rather than by spying on `removeEventListener`: the capture flag has
    // to match the one each was added with, and a spy that only counts calls would pass with the
    // flag wrong and the handler still live on `document`.
    const { node, outside } = guarded();
    const close = vi.fn();
    dismiss(node, close).destroy();

    tap(outside);
    press('Escape');
    expect(close, 'a destroyed action is still listening on document').not.toHaveBeenCalled();
  });
});
