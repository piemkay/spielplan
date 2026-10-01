<script>
  // Decision 550: until the set-up is done Rate is this one card. The steps' count and end words
  // are the person's own set, read from the set-up's route; the default set's until it answers.
  import { get } from '$lib/api.js';

  let { earlierRatings = 0 } = $props();

  const COUNTS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
  /** @type {null | {word: string}[]} */
  let steps = $state(null);

  $effect(() => {
    let cancelled = false;
    get('/ladder/setup')
      .then((res) => {
        if (!cancelled && res?.steps?.length) steps = res.steps;
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  });

  const count = $derived(steps ? (COUNTS[steps.length] ?? String(steps.length)) : 'seven');
  const first = $derived(steps?.[0]?.word ?? 'All-time favourite');
  const last = $derived(steps?.at(-1)?.word ?? 'Hated it');
</script>

<section class="closed" data-testid="rate-before-setup">
  <h2>Rate on your own ladder.</h2>
  <p class="why">
    Each film you've seen goes on one of {count} steps, from {first} down to {last}&nbsp;— one tap
    each. First, a one-time set-up: for each step, tap the films of yours that belong there. It takes
    about a minute.
  </p>
  {#if earlierRatings > 0}
    <p class="footnote" data-testid="rate-earlier">
      Your {earlierRatings} earlier {earlierRatings === 1 ? 'rating is' : 'ratings are'} kept as history.
      Your suggestions start again from your ladder, so the first few evenings are a little less
      personal.
    </p>
  {/if}
  <a class="btn-primary cta" href="/rate/setup" data-testid="rate-setup-cta">Set up my ladder</a>
</section>

<style>
  .closed {
    margin-top: 8px;
    padding: var(--card-pad);
    border-radius: var(--r-md);
    background: var(--surface-1);
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  h2 {
    margin: 0;
    font-size: var(--fs-section);
    line-height: 25px;
    font-weight: 600;
  }
  p {
    margin: 0;
  }
  .cta {
    min-height: var(--touch);
    text-decoration: none;
  }
</style>
