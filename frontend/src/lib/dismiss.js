/**
 * One dismissal, for every popover, menu and sheet. Spec v2.1 §6 preamble; proposals 127 and 131.
 *
 * Proposal 131: "Every popover, menu and sheet dismisses on outside click and on Escape — the
 * prototype has neither, and an implementation copying it ships a menu you cannot click away."
 * Proposal 127 puts dismissal and the focus ring in the same clause of baseline hygiene, and
 * `design.css` now answers the focus half once for the whole app; this is the other half, decided
 * the same way and in one place. The three surfaces that needed it had the same non-answer each —
 * a close control and nothing else — which on §6 preamble's primary form factor means the only
 * way out of a full-bleed overlay is one small target in a corner.
 *
 * POINTERDOWN, NOT CLICK. The outside tap must be read from the touch that starts it. A tap that
 * begins on the backdrop and drifts a few pixels is a scroll to the engine and produces no
 * `click` at all, and iOS Safari withholds `click` from plain non-interactive elements unless
 * they opt in — so a `click` listener would dismiss on desktop and, silently, not on the phone.
 * `pointerdown` is the one signal every engine sends for "the finger landed there".
 *
 * IN THE CAPTURE PHASE, because a dismissal that a stray `stopPropagation` can defeat is the bug
 * rather than the fix. Escape stays in the bubble phase for the mirror reason: a control that
 * legitimately owns Escape — a field clearing itself — must still be able to take it first.
 *
 * `composedPath()` rather than `node.contains(event.target)`, and the reason is the PHASE above
 * rather than any control in this app. The path is fixed at dispatch; `contains` re-reads the
 * tree when the handler runs. In the capture phase on `document` nothing can have moved yet —
 * this listener runs before every element handler in the path, so the two agree — which is
 * exactly why the choice is worth making here and not somewhere it would show: move this
 * registration to the bubble phase, as the Escape listener beside it legitimately is, and
 * `contains` starts answering "outside" for a tap that landed inside a control whose own handler
 * has since re-rendered it away. `composedPath()` is the spelling that keeps the rule true
 * whatever phase it is read in, and the app has no shadow DOM, so that is its whole advantage.
 *
 * ESCAPE IS GLOBAL TO EVERY MOUNTED INSTANCE, and that is a decision rather than an oversight.
 * Each application registers its own `document` listener and none of them knows whether it is the
 * topmost, so two overlays mounted at once both answer one Escape. There is exactly one reachable
 * way to have two in this app: `+layout.svelte`'s `m` shortcut sits on `<svelte:window
 * onkeydown>` and skips only INPUT/TEXTAREA/SELECT/contentEditable, so with §6.7's "show the
 * model" on, a title panel opened from Home and `m` pressed against the focused poster button
 * puts the rail over the panel. Proposal 131 asks that every popover, menu and sheet dismiss on
 * Escape and both of them do; a module-level stack that let only the last-mounted node answer
 * would be a rule the spec does not ask for, added to the one file every overlay depends on, and
 * nothing is written either way — the card is one tap from being open again.
 *
 * The POINTERDOWN half is not the same question and is already right: a tap inside overlay A
 * really is outside overlay B, so dismissing B is the rule holding rather than failing. The case
 * below mounts two nodes and holds the Escape behaviour where a reader will meet it, because a
 * stack introduced later would otherwise land with every test in the suite still green — the
 * three surfaces' own vitest files each mount one overlay.
 * [proposal 131; review cycle 3: M415-C3-COMP-04]
 *
 * The callback is handed the event, so a node whose opener lives OUTSIDE it can tell an opener
 * tap from every other outside tap. `AccountChip` does not need that — the action goes on `.wrap`
 * with the chip button inside it, because an outside-handler scoped to `.menu` alone would close
 * and reopen on the same tap — but `ModelRail`'s trigger is in the shell header, and a toggle
 * that closes on pointerdown and reopens on click is a button that no longer closes.
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
     * Two of the three call sites pass a prop straight through, so the callback can be replaced
     * under us without the node ever being torn down.
     *
     * @param {(event: Event) => void} next
     */
    update(next) {
      onClose = next;
    },
    destroy() {
      // Both, each with the capture flag it was added with — a listener removed with the wrong
      // flag is not removed at all, and these two live on `document`, which outlives every node
      // this action is ever applied to.
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', escape);
    }
  };
}
