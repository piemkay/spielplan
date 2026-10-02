<script>
  // One choice from a short list, with Cancel apart (decision 527): moving a title to a tier,
  // confirming a delete.
  import Sheet from './Sheet.svelte';

  /** @type {{ open: boolean, title?: string, options: Array<{ label: string, detail?: string, checked?: boolean, destructive?: boolean, onSelect: () => void }>, onClose: () => void }} */
  let { open = false, title = '', options = [], onClose } = $props();

  // The choice runs once the sheet's history entry is gone: WebKit drops a navigation started in the
  // same task as the sheet's own Back.
  let chosen = null;
  function closed() {
    const run = chosen;
    chosen = null;
    run?.();
    onClose?.();
  }
</script>

<Sheet {open} onClose={closed} label={title || 'Choose'} detent="fit" plain>
  {#snippet children(close)}
    <div class="group" role="menu">
      {#if title}<p class="title">{title}</p>{/if}
      {#each options as option (option.label)}
        <button
          class="option"
          class:destructive={option.destructive}
          role="menuitem"
          aria-current={option.checked ? 'true' : undefined}
          onclick={() => {
            chosen = option.onSelect;
            close();
          }}
        >
          <span class="label">{option.label}</span>
          {#if option.detail}<span class="detail">{option.detail}</span>{/if}
          {#if option.checked}
            <svg class="check" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
          {/if}
        </button>
      {/each}
    </div>
    <button class="option cancel" onclick={close}>Cancel</button>
  {/snippet}
</Sheet>

<style>
  .group,
  .cancel {
    border-radius: var(--r-md);
    background: var(--surface-1);
    overflow: hidden;
  }
  .group {
    display: flex;
    flex-direction: column;
    margin-bottom: 8px;
  }
  .title {
    margin: 0;
    padding: 14px 16px 10px;
    text-align: center;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .option {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    width: 100%;
    min-height: 56px;
    border: none;
    background: none;
    color: var(--accent-text);
    font-size: var(--fs-section);
    line-height: 25px;
  }
  .group > .option + .option,
  .title + .option {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .detail {
    color: var(--text-3);
  }
  .check {
    position: absolute;
    right: 16px;
  }
  .destructive {
    color: var(--negative);
  }
  .option.cancel {
    background: var(--surface-1);
    font-weight: 600;
  }
</style>
