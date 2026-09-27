<script>
  // One title per card, armed by playback; its first tap writes `seen`, and "no" writes an explicit
  // `unseen` (decision 211). No inline verdict chips: Home has no rate session.
  import { onMount } from 'svelte';
  import { get, post } from '$lib/api.js';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { syncNote } from '$lib/titleCard.js';

  // Home re-reads its shelves here: either answer changes the server-rendered banner.
  let { onAnswered = null } = $props();

  let queue = $state([]);
  let busy = $state(false);
  let failure = $state('');
  let answered = $state(null);
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
</script>

{#if current}
  <div class="card prompt" role="status" data-finish-prompt={current.title_id}>
    <!-- Keyed on `title_id` by hand: this row's own `id` is the prompt's. -->
    <div class="thumb">
      <RatePoster title={{ title_id: current.title_id, name: current.name }} showName={false} />
    </div>
    <div class="text">
      <p class="q">Did you finish <strong>{current.name}</strong>?</p>
      <p class="footnote">
        Jellyfin saw it play to {Math.round((current.progress ?? 0) * 100)}%. Nothing changes until
        you answer: yes marks it seen, no marks it not seen.
      </p>
      {#if failure}<p class="footnote failure" role="alert">{failure}</p>{/if}
    </div>
    <div class="row">
      <button class="btn-tinted" onclick={() => answer(true)} disabled={busy}>
        Yes — mark it seen
      </button>
      <button class="btn-secondary" onclick={() => answer(false)} disabled={busy}>
        No — not seen
      </button>
    </div>
  </div>
{/if}

<!-- A separate element: the question is over. `/rate?head=` puts this title first in the queue. -->
{#if answered}
  <div
    class="card handoff"
    role="status"
    data-finish-handoff={answered.title_id}
    data-answer={answered.seen ? 'seen' : 'unseen'}
  >
    <div class="text">
      <p class="q">
        {#if answered.seen}
          Marked <strong>{answered.name}</strong> seen.
        {:else}
          <!-- "this viewing": dismissal is per Jellyfin session, so a later viewing asks again. -->
          Marked <strong>{answered.name}</strong> not seen — this viewing will not come back.
        {/if}
      </p>
      {#if answered.note}<p class="footnote">{answered.note}</p>{/if}
    </div>
    {#if answered.seen}
      <div class="row">
        <a
          class="btn-tinted"
          href={`/rate?head=${answered.title_id}`}
          data-testid="finish-prompt-cta"
        >
          Rate it now
        </a>
      </div>
    {/if}
  </div>
{/if}

<style>
  .prompt,
  .handoff {
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px 16px;
    flex-wrap: wrap;
  }
  .thumb {
    width: 44px;
    flex: none;
  }
  .text {
    flex: 1 1 12rem;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  p {
    margin: 0;
  }
  .q {
    font-size: var(--fs-callout);
    line-height: 21px;
  }
  strong {
    font-weight: 600;
  }
  .failure {
    color: var(--negative);
  }
  /* A flex row blockifies the anchor, so design.css's min-height gives it its touch target. */
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
</style>
