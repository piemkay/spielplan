<script>
  // A desktop overlay under (or above) the control that opened it: non-modal and no history entry,
  // closed by a press outside, Escape or a navigation (decision 554). Phones use a Sheet.
  import { untrack } from 'svelte';
  import { beforeNavigate } from '$app/navigation';
  import { dismiss } from '$lib/dismiss.js';

  let { open = false, anchor = null, label, width = 480, onClose, children } = $props();

  const GAP = 8;
  const EDGE = 16;
  // It opens above only when this much does not fit below and more fits above.
  const WANTED = 320;

  let panel = $state();
  let place = $state(null);
  let opener = null;

  function measure() {
    if (!panel || !anchor) return;
    const a = anchor.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const w = Math.min(width, vw - 2 * EDGE);
    const below = vh - a.bottom - GAP - EDGE;
    const above = a.top - GAP - EDGE;
    const up = below < Math.max(panel.scrollHeight, WANTED) && above > below;
    place = {
      left: Math.max(EDGE, Math.min(a.left, vw - EDGE - w)),
      width: w,
      top: up ? null : a.bottom + GAP,
      bottom: up ? vh - a.top + GAP : null,
      max: Math.max(Math.min(up ? above : below, vh - 2 * EDGE), 0)
    };
  }

  // Before the DOM changes, so this is what had focus when it was opened.
  $effect.pre(() => {
    if (open) opener = document.activeElement;
  });

  $effect(() => {
    if (!open || !panel) return;
    const node = panel;
    untrack(measure);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
      place = null;
      // Closed with focus inside it (Escape): focus goes back to what opened it.
      const active = document.activeElement;
      if (opener instanceof HTMLElement && (!active || active === document.body || node.contains(active))) {
        opener.focus({ preventScroll: true });
      }
    };
  });

  beforeNavigate(() => {
    if (open) onClose?.();
  });

  /** @param {Event} event */
  function outside(event) {
    // The anchor's own press toggles it; closing here first would reopen it on the click.
    if (event.type === 'pointerdown' && anchor && event.composedPath().includes(anchor)) return;
    onClose?.();
  }
</script>

{#if open}
  <div
    class="popover"
    role="dialog"
    aria-label={label}
    bind:this={panel}
    use:dismiss={outside}
    style:left={place && `${place.left}px`}
    style:top={place?.top != null ? `${place.top}px` : null}
    style:bottom={place?.bottom != null ? `${place.bottom}px` : null}
    style:width={place && `${place.width}px`}
    style:max-height={place && `${place.max}px`}
  >
    {@render children?.()}
  </div>
{/if}

<style>
  .popover {
    position: fixed;
    z-index: 70;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    overscroll-behavior: contain;
    border-radius: var(--r-md);
    background: var(--surface-1);
    box-shadow: var(--shadow-menu), inset 0 0 0 0.5px var(--line-2);
    animation: appear var(--dur-quick) var(--ease);
  }
  @keyframes appear {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
  }
</style>
