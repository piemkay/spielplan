<script>
  // One title per card, armed by playback; its first tap writes `seen`, and "no" writes an explicit
  // `unseen` (decision 211). Its x answers nothing and puts the question away for good (decision 554).
  import { onMount } from 'svelte';
  import { get, post } from '$lib/api.js';
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { syncNote } from '$lib/titleCard.js';
  import { showToast } from '$lib/toast.svelte.js';

  // Home re-reads its shelves here: either answer changes the server-rendered banner.
  let { onAnswered = null, answered = $bindable(null) } = $props();

  let queue = $state([]);
  let busy = $state(false);
  let failure = $state('');
  const current = $derived(queue[0] ?? null);

  onMount(async () => {
    queue = (await get('/prompts/finish').catch(() => [])) ?? [];
  });

  async function answer(finished) {
    if (!current || busy) return;
    busy = true;
    failure = '';
    answered = null;
    const card = current;
    try {
      const result = await post(`/prompts/finish/${card.id}`, { finished });
      // Only on success: dropped in a `finally`, a failed write looked like a successful one.
      queue = queue.filter((p) => p.id !== card.id);
      // For a series both answers report `synced: true`, so the reason, when there is one, is the
      // honest line; a plain success says nothing more.
      const sync = result?.sync ?? null;
      answered = {
        title_id: card.title_id,
        name: card.name,
        seen: finished,
        note: sync && (sync.reason || !sync.synced) ? syncNote(sync) : ''
      };
      onAnswered?.(card.title_id, finished ? 'seen' : 'unseen');
    } catch (err) {
      failure = err.message || 'could not save that — try again';
    } finally {
      busy = false;
    }
  }

  async function close() {
    if (!current || busy) return;
    busy = true;
    failure = '';
    const card = current;
    try {
      await post(`/prompts/finish/${card.id}/close`);
      queue = queue.filter((p) => p.id !== card.id);
      showToast('Removed', { label: 'Undo', run: () => reopen(card) });
    } catch (err) {
      failure = err.message || 'could not put that away — try again';
    } finally {
      busy = false;
    }
  }

  async function reopen(card) {
    try {
      await post(`/prompts/finish/${card.id}/reopen`);
      queue = [card, ...queue.filter((p) => p.id !== card.id)];
    } catch (err) {
      showToast(err.message);
    }
  }
</script>

{#if current}
  <div class="notice-bar wraps" role="status" data-finish-prompt={current.title_id}>
    <!-- Keyed on `title_id` by hand: this row's own `id` is the prompt's. -->
    <div class="thumb">
      <RatePoster title={{ title_id: current.title_id, name: current.name }} showName={false} />
    </div>
    <div class="text">
      <p class="headline">Did you finish {current.name}?</p>
      {#if failure}
        <p class="line failure" role="alert">{failure}</p>
      {:else}
        <p class="line">Jellyfin saw it play to {Math.round((current.progress ?? 0) * 100)}%.</p>
      {/if}
    </div>
    <div class="act">
      <button class="pill" onclick={() => answer(true)} disabled={busy}>Yes — mark it seen</button>
      <button class="pill plain" onclick={() => answer(false)} disabled={busy}>No — not seen</button>
    </div>
    <button class="x" aria-label="Dismiss {current.name}" onclick={close} disabled={busy}>
      <Icon name="close" size={18} />
    </button>
  </div>
{/if}

<!-- A separate element: the question is over. `/rate?head=` puts this title first in the queue. -->
{#if answered}
  <div
    class="notice-bar wraps"
    role="status"
    data-finish-handoff={answered.title_id}
    data-answer={answered.seen ? 'seen' : 'unseen'}
  >
    <div class="thumb">
      <RatePoster title={{ title_id: answered.title_id, name: answered.name }} showName={false} />
    </div>
    <div class="text">
      <p class="headline">
        {#if answered.seen}
          Marked {answered.name} seen.
        {:else}
          <!-- "this viewing": dismissal is per Jellyfin session, so a later viewing asks again. -->
          Marked {answered.name} not seen — this viewing will not come back.
        {/if}
      </p>
      {#if answered.note}<p class="line">{answered.note}</p>{/if}
    </div>
    {#if answered.seen}
      <div class="act">
        <a class="pill" href={`/rate?head=${answered.title_id}`} data-testid="finish-prompt-cta">
          Rate it now
        </a>
      </div>
    {/if}
    <button class="x" aria-label="Close" onclick={() => (answered = null)}>
      <Icon name="close" size={18} />
    </button>
  </div>
{/if}
