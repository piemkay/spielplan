// Every JS-started motion asks here first (decision 530). Without matchMedia (node, jsdom) nothing
// moves, so tests never start a motion timer.

/** True under reduced motion, and wherever the preference cannot be read. */
export function still() {
  return !globalThis.matchMedia?.('(prefers-reduced-motion: no-preference)')?.matches;
}

/** A JS duration: `n` ms, or 0 when still. */
export function ms(n) {
  return still() ? 0 : n;
}

/** A committed answer's tick, fired in the tap handler. Android only: iOS has no vibrate. */
export function haptic() {
  globalThis.navigator?.vibrate?.(8);
}

/** FLIP: glide `el` from where `rect` was to where it now sits, over `d` ms. */
export function flipFrom(el, rect, d) {
  const to = el?.getBoundingClientRect();
  if (!to || !rect || !ms(d)) return;
  const [dx, dy] = [rect.left - to.left, rect.top - to.top];
  if (!dx && !dy) return;
  el.animate?.([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], {
    duration: d,
    easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)'
  });
}
