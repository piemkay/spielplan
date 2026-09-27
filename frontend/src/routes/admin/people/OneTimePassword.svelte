<script>
  // Shown once, where it was issued (§6.6): the server keeps only its hash.
  import { session } from '$lib/session.svelte.js';
  import { showToast } from '$lib/toast.svelte.js';
  import Icon from '$lib/components/Icon.svelte';

  let { name, password, onDone } = $props();

  const canShare = typeof navigator !== 'undefined' && typeof navigator.share === 'function';

  async function copy() {
    try {
      await navigator.clipboard.writeText(password);
      showToast('Copied');
    } catch {
      showToast("Couldn't copy it. Select it and copy instead.");
    }
  }

  function share() {
    const where = session.publicUrl ? ` at ${session.publicUrl}` : '';
    // A dismissed share sheet rejects; nothing to say about it.
    navigator
      .share({ text: `Sign in to Spielplan${where} as ${name} with this one-time password: ${password}` })
      .catch(() => {});
  }
</script>

<section class="card otp" role="status" data-testid="user-otp">
  <div class="top">
    <span class="badge warn"><span class="dot"></span>Shown once</span>
    <span class="acts">
      <button class="btn-secondary small" onclick={copy}>
        <Icon name="copy" size={16} />Copy
      </button>
      {#if canShare}
        <button class="btn-secondary small" onclick={share}>
          <Icon name="share" size={16} />Share
        </button>
      {/if}
    </span>
  </div>
  <h3>One-time password for {name}</h3>
  <p class="value" data-testid="user-otp-value">{password}</p>
  <p class="note">{name} picks their own password the first time they sign in.</p>
  <button class="btn-plain done" onclick={onDone}>Done</button>
</section>

<style>
  .otp {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 4px;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
    background: currentColor;
  }
  .acts {
    display: flex;
    gap: 8px;
  }
  .small {
    min-height: 34px;
    padding: 0 14px;
    border-radius: var(--r-pill);
    font-size: var(--fs-subhead);
    font-weight: 600;
    gap: 6px;
  }
  h3 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .value {
    margin: 0;
    font-size: var(--fs-title);
    line-height: 34px;
    font-weight: 600;
    letter-spacing: 0.04em;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
    user-select: all;
  }
  .note {
    margin: 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .done {
    align-self: flex-start;
    margin-left: -8px;
    font-weight: 600;
  }
</style>
