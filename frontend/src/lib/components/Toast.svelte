<script>
  import { hideToast, toast } from '$lib/toast.svelte.js';

  // The last message, so its words leave with the box rather than vanishing before it.
  let shown = $state({ id: 0, message: '', actionLabel: '' });
  $effect.pre(() => {
    if (toast.message) shown = { id: toast.id, message: toast.message, actionLabel: toast.actionLabel };
  });
</script>

<div class="toast" class:open={!!toast.message} data-testid="toast">
  {#key shown.id}
    <div class="body">
      <span class="message">{shown.message}</span>
      {#if shown.actionLabel}
        <button
          class="btn-plain hit"
          onclick={() => {
            const run = toast.action;
            hideToast();
            run?.();
          }}>{shown.actionLabel}</button
        >
      {/if}
    </div>
  {/key}
</div>
<!-- Always mounted: a status inserted already filled is often not announced. -->
<p class="sr-only" role="status">{toast.message}</p>

<style>
  .toast {
    position: fixed;
    left: max(12px, env(safe-area-inset-left));
    right: max(12px, env(safe-area-inset-right));
    bottom: calc(var(--tabbar) + env(safe-area-inset-bottom) + 12px);
    z-index: 90;
    display: flex;
    min-height: 50px;
    padding: 0 6px 0 16px;
    border-radius: var(--r-md);
    background: var(--surface-3);
    box-shadow: var(--shadow-menu);
    font-size: var(--fs-subhead);
    line-height: 20px;
    visibility: hidden;
    opacity: 0;
    transform: translateY(8px);
    transition: opacity var(--dur-quick) var(--ease-in), transform var(--dur-quick) var(--ease-in),
      visibility 0s linear var(--dur-quick);
  }
  .toast.open {
    --enter-y: 12px;
    --enter-s: 0.98;
    visibility: visible;
    opacity: 1;
    transform: none;
    transition: none;
    animation: enter 260ms var(--ease-spring);
  }
  /* A new message while open: only the words cross over. */
  .body {
    --enter-y: 0;
    --enter-s: 1;
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    animation: enter var(--dur-quick) var(--ease);
  }
  .message {
    flex: 1;
    min-width: 0;
  }
  .btn-plain {
    min-height: 38px;
    font-size: var(--fs-subhead);
    font-weight: 600;
  }
  @media (min-width: 721px) {
    .toast {
      left: auto;
      right: 32px;
      bottom: 32px;
      width: 380px;
    }
  }
</style>
