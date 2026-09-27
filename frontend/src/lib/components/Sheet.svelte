<script>
  // A sheet is a history entry (§6 preamble, decision 527): Back closes it, and so do the scrim,
  // Escape, the close button and a downward swipe. On a desktop it is a centred panel.
  import { tick } from 'svelte';
  import { pushState } from '$app/navigation';
  import { page } from '$app/stores';

  let {
    open = false,
    onClose,
    label,
    detent = 'large',
    width = 560,
    plain = false,
    header = undefined,
    children
  } = $props();

  const key = `sheet-${Math.random().toString(36).slice(2, 10)}`;
  let panel = $state();
  let pushed = $state(false);
  let dragY = $state(0);
  let dragFrom = null;
  let opener = null;

  const stack = $derived($page.state?.sheets ?? []);

  $effect(() => {
    if (!open || pushed) return;
    pushed = true;
    opener = document.activeElement;
    pushState('', { ...$page.state, sheets: [...stack, key] });
    document.documentElement.style.overflow = 'hidden';
    tick().then(() => panel?.focus({ preventScroll: true }));
  });

  // Back took our entry away: close without touching history again.
  $effect(() => {
    if (pushed && !stack.includes(key)) settle();
  });

  // Closed from outside while our entry is still on top: drop it.
  $effect(() => {
    if (!open && pushed && stack.at(-1) === key) history.back();
  });

  function settle() {
    pushed = false;
    dragY = 0;
    document.documentElement.style.overflow = '';
    if (opener instanceof HTMLElement) opener.focus({ preventScroll: true });
    if (open) onClose?.();
  }

  function close() {
    if (pushed && stack.at(-1) === key) history.back();
    else settle();
  }

  function onkeydown(event) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
    }
  }

  function grab(event) {
    dragFrom = event.clientY;
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }
  function drag(event) {
    if (dragFrom === null) return;
    dragY = Math.max(0, event.clientY - dragFrom);
  }
  function release() {
    if (dragFrom === null) return;
    dragFrom = null;
    if (dragY > 96) close();
    else dragY = 0;
  }
</script>

{#if open}
  <div class="layer" class:plain>
    <button class="scrim" aria-label="Close" tabindex="-1" onclick={close}></button>
    <div
      class="panel {detent}"
      class:plain
      role="dialog"
      aria-modal="true"
      aria-label={label}
      tabindex="-1"
      bind:this={panel}
      {onkeydown}
      style:--w="{width}px"
      style:transform={dragY ? `translateY(${dragY}px)` : null}
    >
      {#if !plain}
        <div
          class="grab"
          role="presentation"
          onpointerdown={grab}
          onpointermove={drag}
          onpointerup={release}
          onpointercancel={release}
        >
          <span class="grabber" aria-hidden="true"></span>
          {#if header}{@render header(close)}{/if}
        </div>
      {/if}
      <div class="content">{@render children(close)}</div>
    </div>
  </div>
{/if}

<style>
  .layer {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: flex;
    align-items: flex-end;
    justify-content: center;
  }
  .scrim {
    position: absolute;
    inset: 0;
    border: none;
    padding: 0;
    background: var(--scrim);
    cursor: default;
    animation: fade 0.2s var(--ease);
  }
  .panel {
    position: relative;
    width: 100%;
    display: flex;
    flex-direction: column;
    background: var(--bg-elevated);
    border-radius: var(--r-lg) var(--r-lg) 0 0;
    box-shadow: var(--shadow-sheet);
    outline: none;
    animation: rise 0.34s var(--ease);
    transition: transform 0.2s var(--ease);
  }
  .panel.large {
    height: calc(100dvh - env(safe-area-inset-top) - 12px);
  }
  .panel.medium {
    max-height: 72dvh;
  }
  .panel.plain {
    background: none;
    box-shadow: none;
    padding: 0 8px calc(8px + env(safe-area-inset-bottom));
  }
  .grab {
    flex: none;
    touch-action: none;
    padding: 6px var(--gutter) 0;
  }
  .grabber {
    display: block;
    width: 36px;
    height: 5px;
    margin: 0 auto 6px;
    border-radius: var(--r-pill);
    background: rgba(245, 240, 232, 0.28);
  }
  .content {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 0 var(--gutter) calc(24px + env(safe-area-inset-bottom));
  }
  .plain .content {
    padding: 0;
    overflow: visible;
  }

  @media (min-width: 721px) {
    .layer {
      align-items: center;
    }
    .panel,
    .panel.large,
    .panel.medium {
      width: min(var(--w), calc(100% - 48px));
      height: auto;
      max-height: 86dvh;
      border-radius: var(--r-lg);
    }
    .panel.plain {
      width: min(400px, calc(100% - 48px));
    }
    .grabber {
      visibility: hidden;
    }
  }

  @keyframes rise {
    from {
      transform: translateY(24px);
      opacity: 0;
    }
  }
  @keyframes fade {
    from {
      opacity: 0;
    }
  }
</style>
