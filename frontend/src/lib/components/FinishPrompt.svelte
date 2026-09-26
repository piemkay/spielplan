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
  <div class="prompt" role="status" data-finish-prompt={current.title_id}>
    <!-- Keyed on `title_id` by hand: this row's own `id` is the prompt's. -->
    <div class="thumb">
      <RatePoster title={{ title_id: current.title_id, name: current.name }} showName={false} />
    </div>
    <div class="text">
      <div class="q">Did you finish <strong>{current.name}</strong>?</div>
      <div class="data why">
        Jellyfin saw it play to {Math.round((current.progress ?? 0) * 100)}%. Either answer is
        recorded — yes marks it seen, no marks it not seen — and nothing is written until you
        answer.
      </div>
      {#if failure}<div class="data failure" role="alert">{failure}</div>{/if}
    </div>
    <div class="row">
      <button class="btn-primary" onclick={() => answer(true)} disabled={busy}>
        Yes — mark it seen
      </button>
      <button class="btn-ghost" onclick={() => answer(false)} disabled={busy}>
        No — not seen
      </button>
    </div>
  </div>
{/if}

<!-- A separate element: the question is over. `/rate?head=` puts this title first in the queue. -->
{#if answered}
  <div
    class="handoff"
    role="status"
    data-finish-handoff={answered.title_id}
    data-answer={answered.seen ? 'seen' : 'unseen'}
  >
    <div class="text">
      <div class="q">
        {#if answered.seen}
          Marked <strong>{answered.name}</strong> seen.
        {:else}
          <!-- "this viewing": dismissal is per Jellyfin session, so a later viewing asks again. -->
          Marked <strong>{answered.name}</strong> not seen — this viewing will not come back.
        {/if}
      </div>
      {#if answered.note}<div class="data why">{answered.note}</div>{/if}
    </div>
    {#if answered.seen}
      <div class="row">
        <a
          class="btn-primary"
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
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    padding: 12px 15px;
    margin-bottom: 14px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
  }
  .thumb {
    width: 44px;
    flex: none;
  }
  .text {
    flex: 1 1 12rem;
    min-width: 0;
  }
  .q {
    font-size: 14px;
  }
  .why {
    margin-top: 3px;
  }
  .failure {
    margin-top: 4px;
    color: var(--ember-lift);
  }
  /* A flex row blockifies the anchor, so design.css's min-height gives it its touch target. */
  .row {
    display: flex;
    gap: 8px;
  }
</style>
