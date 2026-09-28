<script>
  import { hideToast, toast } from '$lib/toast.svelte.js';
</script>

{#if toast.message}
  {#key toast.id}
    <div class="toast" role="status" data-testid="toast">
      <span class="message">{toast.message}</span>
      {#if toast.action}
        <button
          class="btn-plain hit"
          onclick={() => {
            const run = toast.action;
            hideToast();
            run?.();
          }}>{toast.actionLabel}</button
        >
      {/if}
    </div>
  {/key}
{/if}

<style>
  .toast {
    position: fixed;
    left: max(12px, env(safe-area-inset-left));
    right: max(12px, env(safe-area-inset-right));
    bottom: calc(var(--tabbar) + env(safe-area-inset-bottom) + 12px);
    z-index: 90;
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 50px;
    padding: 0 6px 0 16px;
    border-radius: var(--r-md);
    background: var(--surface-3);
    box-shadow: var(--shadow-menu);
    font-size: var(--fs-subhead);
    line-height: 20px;
    animation: fadeIn 0.2s var(--ease);
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
