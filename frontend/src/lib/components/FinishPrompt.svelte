<script>
  /**
   * §7.3's finish prompt. Spec v2.1 §7.3, decision-doc proposal 150.
   *
   * "≥ 90% playback … arms a per-user prompt — 'Did you finish X?' → one tap sets `seen` …
   *  when undeliverable, the prompt queues and surfaces as an in-app banner on next open. The
   *  banner path is the whole M1 behaviour."
   *
   * Proposal 150 separates this from §6.0's pending-verdicts banner, and the separation is
   * visible here: this card names exactly **one** title, is armed by a playback event, and its
   * first tap is the one thing on the playback path that writes `seen`. The Home banner (M2)
   * names up to three titles that are already seen and never writes state at all.
   *
   * One at a time, on purpose. A stack of four "did you finish…?" cards on a Saturday morning
   * is a chore; one is a question.
   */
  import { onMount } from 'svelte';
  import { get, post } from '$lib/api.js';

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
    const answered = current;
    try {
      await post(`/prompts/finish/${answered.id}`, { finished });
      // Only on success. Dropping the card in a `finally` made a failed write look exactly
      // like a successful one: the banner said "Yes — mark it seen", vanished, and nothing
      // was marked.
      queue = queue.filter((p) => p.id !== answered.id);
    } catch (err) {
      failure = err.message || 'could not save that — try again';
    } finally {
      busy = false;
    }
  }
</script>

{#if current}
  <div class="prompt" role="status" data-finish-prompt={current.title_id}>
    <div class="text">
      <div class="q">Did you finish <strong>{current.name}</strong>?</div>
      <div class="data why">
        Jellyfin saw it play to {Math.round((current.progress ?? 0) * 100)}%. Nothing is marked
        until you say so.
      </div>
      {#if failure}<div class="data failure" role="alert">{failure}</div>{/if}
    </div>
    <div class="row">
      <button class="btn-primary" onclick={() => answer(true)} disabled={busy}>
        Yes — mark it seen
      </button>
      <button class="btn-ghost" onclick={() => answer(false)} disabled={busy}>Not yet</button>
    </div>
  </div>
{/if}

<style>
  .prompt {
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
  .row {
    display: flex;
    gap: 8px;
  }
</style>
