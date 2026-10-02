<script>
  // Try a twist (decision 560 item 7, board MA-4): up to three groups of the member's own films
  // placed high, each kept by the server only where the twisted recipe still leaves ten films in
  // the library. A tap adds one with an Undo; the shuffle asks for the next set.
  import { untrack } from 'svelte';
  import { get, qs } from '$lib/api.js';
  import { plural } from '$lib/home.svelte.js';
  import { applyTwist, groupOf } from '$lib/recipe.svelte.js';
  import { showToast } from '$lib/toast.svelte.js';
  import Icon from './Icon.svelte';
  import RatePoster from './RatePoster.svelte';

  let { kind = 'movie', query = {} } = $props();

  let seed = $state(0);
  let twists = $state([]);
  const key = $derived(JSON.stringify([kind, query]));

  let seq = 0;
  $effect(() => {
    key;
    untrack(() => load(0));
  });

  async function load(next) {
    const mine = ++seq;
    const res = await get(`/mix/twists${qs({ ...query, kind, seed: next })}`).catch(() => null);
    if (mine !== seq) return;
    seed = next;
    twists = res?.twists ?? [];
  }

  function add(twist) {
    const undo = applyTwist(twist);
    showToast(`Added ${twist.group_name} like ${twist.name}`, { label: 'Undo', run: undo });
  }

  const terms = (twist) => twist.terms.map((t) => t.label).join(', ');
  const SKIPPED = 'Other twists. Twists that leave fewer than 10 films are skipped';
</script>

{#if twists.length}
  <section class="twists" aria-label="Try a twist" data-testid="twist-row">
    <div class="top">
      <h2><span class="lead">Try a twist</span> <span aria-hidden="true">·</span> from films you placed high</h2>
      <button class="shuffle" aria-label={SKIPPED} title={SKIPPED} onclick={() => load(seed + 1)}>
        <Icon name="shuffle" size={20} />
      </button>
    </div>
    <div class="cards" data-nobar>
      {#each twists as t (`${t.title_id}:${t.group}`)}
        <button
          class="twist press"
          data-testid="twist"
          aria-label="Add {t.group_name} like {t.name}, {t.library_n} {plural(kind, t.library_n)} in your library fit: {terms(t)}"
          onclick={() => add(t)}
        >
          <Icon name="plus" size={16} />
          <span class="thumb" aria-hidden="true"><RatePoster title={{ title_id: t.title_id, name: t.name }} showName={false} /></span>
          <span class="text">
            <span class="name">
              <span class="dot" style:background={groupOf(t.group)?.colour ?? t.colour} aria-hidden="true"></span>
              <span>{t.group_name} like {t.name}</span>
            </span>
            <span class="sub"><span class="n">{t.library_n} fit</span> · {terms(t)}</span>
          </span>
        </button>
      {/each}
    </div>
  </section>
{/if}

<style>
  .twists {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 20px;
  }
  .top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 32px;
  }
  h2 {
    margin: 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 400;
    color: var(--text-3);
  }
  .lead {
    color: var(--text-2);
    font-weight: 600;
  }
  .shuffle {
    position: relative;
    flex: none;
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    margin: -6px -10px -6px 0;
    padding: 0;
    border: none;
    border-radius: var(--r-pill);
    background: none;
    color: var(--text-2);
  }
  /* A phone scrolls the twists sideways; from 721 px they wrap. */
  .cards {
    display: flex;
    gap: 8px;
    margin: 0 calc(-1 * var(--gutter));
    padding: 0 var(--gutter);
    overflow-x: auto;
    scrollbar-width: none;
  }
  .twist {
    --press: 0.97;
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    max-width: 100%;
    height: 50px;
    padding: 0 16px 0 10px;
    border: 1px dashed rgba(245, 240, 232, 0.42);
    border-radius: var(--r-pill);
    background: none;
    color: var(--text);
    text-align: left;
  }
  .twist > :global(svg) {
    color: var(--text-2);
  }
  .thumb {
    flex: none;
    width: 18px;
    border-radius: 3px;
    overflow: hidden;
  }
  .thumb :global(.poster) {
    border-radius: 0;
  }
  .text {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .name {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
    white-space: nowrap;
  }
  .name > span:last-child {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: var(--r-pill);
  }
  .sub {
    max-width: 220px;
    font-size: var(--fs-footnote);
    line-height: 16px;
    color: var(--text-3);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .n {
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  @media (min-width: 721px) {
    .cards {
      flex-wrap: wrap;
      margin: 0;
      padding: 0;
      overflow: visible;
    }
  }
</style>
