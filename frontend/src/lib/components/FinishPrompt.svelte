<script>
  /**
   * §7.3's finish prompt. Spec v2.1 §7.3, §6.1; decisions 210, 211 and 212.
   *
   * "≥ 90% playback … arms a per-user prompt — 'Did you finish X?' → one tap sets `seen` and
   *  offers the verdict flow. When undeliverable, the prompt queues and surfaces as an in-app
   *  banner on next open. The banner path is the whole M1 behaviour."
   *
   * Proposal 150 separates this from §6.0's pending-verdicts banner, and the separation is
   * visible here: this card names exactly **one** title, is armed by a playback event, and its
   * first tap is the one thing on the playback path that writes `seen`. The Home banner (M2)
   * names up to three titles that are already seen and never writes state at all.
   *
   * TWO CLAUSES OF §7.3 WERE MISSING, and decision 212 says what to do about them. "Offers the
   * verdict flow" had no affordance at all — the card posted, dropped the row, and offered
   * nothing; and proposal 150's own substitute (the answered title "leaves a title in the
   * banner's population") was undelivered, because the banner is server-rendered from
   * `/api/home` and this component was mounted with no callback while the identical seen write
   * from the title card already re-read it. So: the queue link in §6.0's banner-CTA shape, with
   * this title at the head of §6.1's queue, and `onAnswered` for the surface that owns the
   * banner. Deliberately NOT inline verdict chips (decision 212): they would put a second
   * observation writer on Home with no rate session, no card token and no §6.1 block counter.
   *
   * AND "NO" IS AN ANSWER THAT WRITES (decision 211). `api/state.py` records a decline as an
   * explicit `unseen` now, because the absent row was what the 15-minute sweep adopted Jellyfin's
   * Played flag into — the state a member had just declined arrived anyway within the quarter
   * hour. The copy has to say so: a card that promises "nothing is marked" and then writes is
   * worse than one that asks plainly.
   *
   * One question at a time, on purpose. A stack of four "did you finish…?" cards on a Saturday
   * morning is a chore; one is a question.
   */
  import { onMount } from 'svelte';
  import { get, post } from '$lib/api.js';

  /**
   * Called after a successful answer, with the title and the state that was written. Home wires
   * it to its own `loadShelves()` (decision 212): both answers change §6.0's banner population —
   * a "yes" puts a seen-and-unrated title into it, a "no" keeps one out — and the banner is the
   * server's sentence, so the only way to refresh it is to ask again.
   */
  let { onAnswered = null } = $props();

  let queue = $state([]);
  let busy = $state(false);
  let failure = $state('');
  /** The title just answered, which is what the handoff below is about. */
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
      // Only on success. Dropping the card in a `finally` made a failed write look exactly
      // like a successful one: the banner said "Yes — mark it seen", vanished, and nothing
      // was marked.
      queue = queue.filter((p) => p.id !== card.id);
      // `sync` travels for both answers now (§6.7's rail prints these reasons verbatim), and for
      // a series a "seen" that reached Jellyfin and an `unseen` that deliberately did not both
      // report `synced: true` — so the reason, when there is one, is the honest line.
      const sync = result?.sync ?? null;
      answered = {
        title_id: card.title_id,
        name: card.name,
        seen: finished,
        note: sync ? (sync.reason ?? (sync.synced ? '' : 'your Jellyfin was not told')) : ''
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

<!-- The handoff, and a separate element from the card on purpose: the question is over, so
     `[data-finish-prompt]` is gone and what is left is one line and one link. §7.3's "offers the
     verdict flow", in the shape §6.0's banner CTA already uses — `/rate?head=<id>` enters §6.1's
     queue with this title at its head, because "a prompt that names titles and then presents a
     different one is worse than no prompt" (proposal 150, kept by decision 212). Nothing to rate
     after a "no", so that branch is the line alone. -->
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
          <!-- "this viewing", not "this title": the dismissal guard is keyed on the Jellyfin
               session id (`0006_jellyfin.sql`), so a later viewing asks again. -->
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
  /* A flex row, which is also what gives the anchor its touch target: design.css grows every
     `.btn-primary` to `--touch` on a coarse pointer, and `min-height` does nothing to an inline
     element — as a flex item it is blockified. Same reason §6.0's banner CTA works. */
  .row {
    display: flex;
    gap: 8px;
  }
</style>
