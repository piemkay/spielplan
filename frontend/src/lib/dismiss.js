/**
 * Outside pointerdown in the capture phase (iOS withholds `click`, and no stopPropagation may
 * defeat it); Escape in the bubble phase, so a field can take it first.
 *
 * @param {HTMLElement} node
 * @param {(event: Event) => void} close
 */
export function dismiss(node, close) {
  let onClose = close;

  /** @param {Event} event */
  const outside = (event) => {
    if (event.composedPath().includes(node)) return;
    onClose?.(event);
  };

  /** @param {KeyboardEvent} event */
  const escape = (event) => {
    if (event.key !== 'Escape') return;
    onClose?.(event);
  };

  document.addEventListener('pointerdown', outside, true);
  document.addEventListener('keydown', escape);

  return {
    /**
     * Call sites pass a prop through, so the callback can change without a teardown.
     *
     * @param {(event: Event) => void} next
     */
    update(next) {
      onClose = next;
    },
    destroy() {
      // Each with the capture flag it was added with, or it is not removed.
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', escape);
    }
  };
}
