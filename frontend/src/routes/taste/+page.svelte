<script>
  // Your taste (§6.5): the DNA terms that sit high on the person's own ladder and those that land lower,
  // each read against their own middle. Opened from You, never a tab.
  import { onMount } from 'svelte';
  import { returningCard } from '$lib/cardJump.js';
  import { avatarColour } from '$lib/components/Avatar.svelte';
  import TasteRow from '$lib/components/TasteRow.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';
  import { modelGate } from '$lib/home.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import { KINDS, NOUNS, loadTaste, ownGroups, taste } from '$lib/taste.svelte.js';
  import { topbar } from '$lib/topbar.svelte.js';

  let data = $state(null);
  let error = $state('');
  let all = $state(false);
  let selected = $state(null);
  let ticket = 0;

  const noun = $derived(NOUNS[taste.kind]);
  const initial = $derived((session.user?.name ?? '?').charAt(0).toUpperCase());
  const colour = $derived(avatarColour(session.user));
  const marksFor = (row) => [{ pos: row.pos, initials: initial, colour, who: 'You' }];
  const LOWER = 'Lower only means you may enjoy that kind of night a little less than usual.';
  const HINT = 'Tap a term to see your films that have it.';
  const sections = $derived.by(() => {
    if (!data) return [];
    if (all) return [{ head: `All ${data.n_terms}, high to low`, rows: data.all, foot: `${LOWER} ${HINT}` }];
    return [
      { head: 'Sits high for you', rows: data.high, foot: HINT },
      { head: 'Lands lower for you', rows: data.low, foot: LOWER }
    ].filter((s) => s.rows.length);
  });

  // Re-read on a kind and when Show the numbers settles: the numbers are absent from the payload.
  $effect(() => {
    const kind = taste.kind;
    void modelGate.epoch;
    read(kind);
  });

  // Back from Home's grid reopens the card the jump there left from (decision 557 item 6).
  onMount(() => {
    const back = returningCard('taste');
    if (back !== null) selected = { id: back };
  });

  async function read(kind) {
    const mine = ++ticket;
    error = '';
    try {
      const payload = await loadTaste(kind);
      if (mine === ticket) data = payload;
    } catch (err) {
      if (mine === ticket) error = err.message;
    }
  }

  function choose(kind) {
    if (taste.kind === kind) return;
    data = null;
    all = false;
    taste.kind = kind;
  }

  $effect(() => {
    if (!topbar.host) return;
    topbar.content = bar;
    return () => {
      if (topbar.content === bar) topbar.content = null;
    };
  });
</script>

{#snippet bar()}
  <div class="top">
    <div class="segmented kinds" role="group" aria-label="Kind">
      {#each Object.entries(KINDS) as [key, label] (key)}
        <button aria-pressed={taste.kind === key} onclick={() => choose(key)}>{label}</button>
      {/each}
    </div>
    <span class="grow"></span>
    <a class="btn-plain compare" href="/taste/compare" data-testid="taste-compare">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="9" r="3" /><path d="M3.5 19a5.5 5.5 0 0 1 11 0" /><circle cx="16.5" cy="8" r="2.5" /><path d="M16 13.5a4.5 4.5 0 0 1 5 4.5" /></svg>
      <span>Compare</span>
    </a>
  </div>
{/snippet}

<section class="taste" data-testid="taste-surface">
  {#if !topbar.host}{@render bar()}{/if}
  <h1 class="large-title">Your taste</h1>

  {#if error}
    <div class="card"><p class="why" role="alert">{error}</p></div>
  {:else if !data}
    <p class="sr-only" role="status">Reading your ladder…</p>
  {:else if !data.n_terms}
    <div class="card empty" data-testid="taste-empty">
      <p class="why">
        Your taste shows once at least 4 of the {noun} on your ladder share a mood, a theme or a style.
      </p>
      <a class="btn-secondary" href="/rate">Go to Rate</a>
    </div>
  {:else}
    <p class="lead">Read from the {data.placed} {noun} on your ladder.</p>
    <div class="legend" aria-hidden="true">
      <span><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5 8 12l7 7" /></svg>Lower</span>
      <span>Middle</span>
      <span>High<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="m9.5 5.5 6.5 6.5-6.5 6.5" /></svg></span>
    </div>
    {#each sections as s, i (s.head)}
      <section class="group" class:first={i === 0}>
        <h2 class="list-header">{s.head}</h2>
        <div class="list-group">
          {#each s.rows as row (row.term)}
            <TasteRow
              {row}
              marks={marksFor(row)}
              onOpen={(film) => (selected = film)}
              expand={(term) => ownGroups(taste.kind, term)}
              line
            />
          {/each}
        </div>
        {#if s.foot}<p class="list-footer">{s.foot}</p>{/if}
      </section>
    {/each}
    {#if all || data.n_terms > data.high.length + data.low.length}
      <button class="btn-secondary more" onclick={() => (all = !all)}>
        {all ? 'Show fewer' : `Show all ${data.n_terms} terms`}
      </button>
    {/if}
  {/if}
</section>

{#if selected}
  <TitleDetail
    titleId={selected.id}
    seed={selected.name ? selected : undefined}
    from="taste"
    onClose={() => (selected = null)}
    onStateChange={() => read(taste.kind)}
  />
{/if}

<style>
  .taste {
    max-width: 640px;
    display: flex;
    flex-direction: column;
  }
  .top {
    display: flex;
    align-items: center;
    gap: 4px;
    min-height: 44px;
  }
  .kinds {
    flex: none;
    width: 150px;
    min-height: 32px;
  }
  .kinds > button {
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .grow {
    flex: 1;
  }
  .compare {
    gap: 6px;
    margin-right: 2px;
  }
  .large-title {
    margin-top: 8px;
  }
  .lead {
    margin: 8px 0 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .empty {
    margin-top: 32px;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .empty .why {
    margin: 0;
  }
  .legend {
    margin-top: 24px;
    width: clamp(96px, calc(100% - 206px), 152px);
    margin-left: var(--gutter);
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    font-size: var(--fs-caption);
    line-height: 18px;
    color: var(--text-3);
    white-space: nowrap;
  }
  .legend > span {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .legend > span:first-child {
    justify-self: start;
  }
  .legend > span:last-child {
    justify-self: end;
  }
  .group {
    margin-top: 32px;
    display: flex;
    flex-direction: column;
  }
  .group.first {
    margin-top: 12px;
  }
  .more {
    width: 100%;
    margin-top: 24px;
    min-height: 48px;
  }
  /* The legend spans the track, which on a desktop takes the width the posters leave. */
  @media (min-width: 721px) {
    .legend {
      width: calc(100% - 2 * var(--gutter) - 208px);
    }
  }
  @media (min-width: 1100px) {
    .taste {
      max-width: 968px;
    }
    .legend {
      width: calc(100% - 2 * var(--gutter) - 468px);
      margin-left: calc(var(--gutter) + 260px);
    }
  }
</style>
