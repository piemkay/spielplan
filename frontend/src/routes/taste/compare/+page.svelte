<script>
  // Compare (§6.5): the Taste chart for any two members, picked in two seats. Each row carries both
  // markers, told apart by their initials; the films behind a term come only to the two compared.
  import Avatar, { avatarColour } from '$lib/components/Avatar.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import TasteRow from '$lib/components/TasteRow.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';
  import { facetColour, modelGate } from '$lib/home.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import {
    KINDS,
    facetsOf,
    gateLine,
    loadCompare,
    loadMembers,
    seatsReady,
    showAll,
    takeSeat,
    taste
  } from '$lib/taste.svelte.js';
  import { topbar } from '$lib/topbar.svelte.js';

  let members = $state(null);
  let seats = $state([null, null]);
  let data = $state(null);
  let gated = $state(false);
  let error = $state('');
  let all = $state(false);
  let sort = $state('diff');
  let facet = $state(null);
  // The seat the picker fills, or null while it is closed.
  let picking = $state(null);
  let selected = $state(null);
  let ticket = 0;

  const viewer = $derived(session.user?.id);
  const seated = $derived(seats.includes(viewer));
  const person = (id) => members?.members.find((m) => m.id === id) ?? null;
  const labelOf = (id) => (id === viewer ? 'You' : (person(id)?.name ?? 'Choose'));
  // Named by person, never by seat, so a swap leaves the copy as it was.
  const pair = $derived(
    seats
      .map((id) => person(id)?.name ?? '')
      .sort()
      .join(' and ')
  );
  const lower = $derived(
    seated
      ? 'Lower only means one of you may enjoy that kind of night a little less than usual.'
      : `Lower only means one of them may enjoy that kind of night a little less than usual. Only ${pair} see the films behind these.`
  );
  const sections = $derived.by(() => {
    if (!data) return [];
    if (all) {
      const list = showAll(data.all, { sort, facet });
      return list.map((s, i) => ({ ...s, foot: i === list.length - 1 ? lower : '' }));
    }
    return [
      { key: 'alike', head: 'Most alike', facet: null, rows: data.alike, foot: data.note ?? '' },
      { key: 'different', head: 'Most different', facet: null, rows: data.different, foot: lower }
    ].filter((s) => s.rows.length);
  });

  function marksFor(row) {
    return seats.map((id, i) => {
      const m = person(id);
      const pos = i === 0 ? row.pa : row.pb;
      return { pos, initials: m?.initials ?? '?', colour: avatarColour(m), who: labelOf(id) };
    });
  }

  // A kind reads its own roster; seats carry over, and an empty one takes the default.
  $effect(() => {
    const kind = taste.kind;
    void modelGate.epoch;
    open(kind);
  });

  async function open(kind) {
    const mine = ++ticket;
    error = '';
    try {
      const roster = await loadMembers(kind);
      if (mine !== ticket) return;
      members = roster;
      if (seats[0] == null || seats[1] == null) seats = roster.default;
      await read(mine, kind);
    } catch (err) {
      if (mine === ticket) error = err.message;
    }
  }

  async function read(mine, kind) {
    gated = !seatsReady(seats, members.members);
    if (gated) {
      data = null;
      return;
    }
    try {
      const payload = await loadCompare(kind, seats[0], seats[1]);
      if (mine === ticket) data = payload;
    } catch (err) {
      if (mine !== ticket) return;
      data = null;
      if (err.detail?.reason === 'not_pickable') gated = true;
      else error = err.message;
    }
  }

  function choose(kind) {
    if (taste.kind === kind) return;
    data = null;
    facet = null;
    taste.kind = kind;
  }

  function take(m) {
    if (!m.pickable || picking === null) return;
    const next = takeSeat(seats, picking, m.id);
    if (next === seats) return;
    // A swap of the two seated people needs no new read: only the sides change.
    const swapped = next[0] === seats[1] && next[1] === seats[0];
    seats = next;
    if (swapped && data) {
      data = {
        ...data,
        a: data.b,
        b: data.a,
        ...Object.fromEntries(
          ['alike', 'different', 'all'].map((k) => [k, data[k].map((r) => ({ ...r, pa: r.pb, pb: r.pa }))])
        )
      };
      return;
    }
    facet = null;
    data = null;
    read(++ticket, taste.kind);
  }

  function showAllTerms(on) {
    all = on;
    sort = 'diff';
    facet = null;
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
    <a class="btn-plain back" href="/taste">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 5 8 12l7 7" /></svg>
      <span>Your taste</span>
    </a>
    <span class="grow"></span>
    <div class="segmented kinds" role="group" aria-label="Kind">
      {#each Object.entries(KINDS) as [key, label] (key)}
        <button aria-pressed={taste.kind === key} onclick={() => choose(key)}>{label}</button>
      {/each}
    </div>
  </div>
{/snippet}

<section class="compare" data-testid="taste-compare-surface">
  {#if !topbar.host}{@render bar()}{/if}
  <h1 class="large-title">Compare</h1>

  {#if members}
    <div class="seats">
      {#each seats as id, i (i)}
        {#if i === 1}<span class="and">and</span>{/if}
        <button
          class="seat"
          aria-haspopup="dialog"
          aria-expanded={picking === i}
          aria-label="Seat {i + 1}: {labelOf(id)}. Change"
          onclick={() => (picking = i)}
          data-testid="taste-seat"
        >
          <Avatar name={person(id)?.name ?? ''} person={person(id)} />
          <span class="name">{labelOf(id)}</span>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5.5 9.5 6.5 6.5 6.5-6.5" /></svg>
        </button>
      {/each}
    </div>
  {/if}

  {#if error}
    <div class="card note"><p class="why" role="alert">{error}</p></div>
  {:else if gated}
    <div class="card note" data-testid="taste-compare-gate">
      <p class="why" aria-live="polite">{gateLine(taste.kind)}</p>
      {#if seated}<a class="btn-secondary" href="/rate">Go to Rate</a>{/if}
    </div>
  {:else if !data}
    <p class="sr-only" role="status">Reading both ladders…</p>
  {:else}
    {#if all}
      <div class="chips" role="group" aria-label="Show terms from" data-nobar>
        <button class="pill" aria-pressed={facet === null} onclick={() => (facet = null)}>All</button>
        {#each facetsOf(data.all) as [key, name] (key)}
          <button class="pill" aria-pressed={facet === key} onclick={() => (facet = key)}>
            <span class="dot" style:background={facetColour(key)}></span>{name}
          </button>
        {/each}
      </div>
      <div class="segmented order" role="group" aria-label="Order">
        <button aria-pressed={sort === 'diff'} onclick={() => (sort = 'diff')}>Most different</button>
        <button aria-pressed={sort === 'alike'} onclick={() => (sort = 'alike')}>Most alike</button>
        <button aria-pressed={sort === 'facet'} onclick={() => (sort = 'facet')}>By facet</button>
      </div>
    {/if}
    <div class="legend" aria-hidden="true">
      <span><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5 8 12l7 7" /></svg>Lower</span>
      <span>Middle</span>
      <span>High<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="m9.5 5.5 6.5 6.5-6.5 6.5" /></svg></span>
    </div>
    {#each sections as s, i (s.key)}
      <section class="group" class:first={i === 0}>
        <h2 class="list-header">
          {#if all && sort === 'facet'}<span class="dot" style:background={facetColour(s.facet)}></span>{/if}
          {s.head}
        </h2>
        <div class="list-group">
          {#each s.rows as row (row.term)}
            <TasteRow {row} marks={marksFor(row)} onOpen={(film) => (selected = film)} />
          {/each}
        </div>
        {#if s.foot}<p class="list-footer">{s.foot}</p>{/if}
      </section>
    {/each}
    {#if all || data.all.length > data.alike.length + data.different.length}
      <button class="btn-secondary more" onclick={() => showAllTerms(!all)}>
        {all ? 'Show fewer' : `Show all ${data.all.length} terms`}
      </button>
    {/if}
  {/if}
</section>

<Sheet open={picking !== null} onClose={() => (picking = null)} label="Choose two to compare" detent="fit" width={480}>
  {#snippet children(close)}
    <div class="bar">
      <h2 class="section-title">Compare</h2>
      <button class="btn-plain" onclick={close}>Done</button>
    </div>
    <div class="picker" data-testid="taste-picker">
      <div class="segmented" role="group" aria-label="Seat to change">
        {#each seats as id, i (i)}
          <button aria-pressed={picking === i} aria-label="Seat {i + 1}: {labelOf(id)}" onclick={() => (picking = i)}>
            <Avatar name={person(id)?.name ?? ''} person={person(id)} size={24} />
            <span class="name">{labelOf(id)}</span>
          </button>
        {/each}
      </div>
      <section>
        <p class="list-header" aria-hidden="true">
          {picking === 1 ? `${labelOf(seats[0])} and …` : `… and ${labelOf(seats[1])}`}
        </p>
        <div class="list-group" role="radiogroup" aria-label="Who to compare">
          {#each members?.members ?? [] as m (m.id)}
            {@const mine = picking !== null && seats[picking] === m.id}
            <button
              class="list-row who"
              role="radio"
              aria-checked={mine}
              aria-disabled={!m.pickable}
              onclick={() => take(m)}
            >
              <span class:dim={!m.pickable}><Avatar name={m.name} person={m} /></span>
              <span class="text">
                <span class="name" class:dim={!m.pickable}>{m.id === viewer ? 'You' : m.name}</span>
                {#if m.reason}
                  <span class="footnote">{m.reason}</span>
                {:else if picking !== null && seats[1 - picking] === m.id}
                  <span class="footnote">In the other seat</span>
                {/if}
              </span>
              {#if mine}
                <svg class="check" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
              {/if}
            </button>
          {/each}
        </div>
        <p class="list-footer">
          Anyone in the household can compare any two. The films behind a term show only to the two being
          compared.
        </p>
      </section>
    </div>
  {/snippet}
</Sheet>

{#if selected}
  <TitleDetail
    titleId={selected.id}
    seed={selected}
    onClose={() => (selected = null)}
    onPerson={() => (selected = null)}
    onStateChange={() => open(taste.kind)}
  />
{/if}

<style>
  .compare {
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
  .back {
    gap: 2px;
    margin-left: -10px;
  }
  .grow {
    flex: 1;
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
  .large-title {
    margin-top: 8px;
  }
  .seats {
    margin-top: 16px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    column-gap: 8px;
    align-items: center;
  }
  .seat {
    min-width: 0;
    height: 56px;
    padding: 0 10px 0 12px;
    border: none;
    border-radius: var(--r-md);
    background: var(--surface-1);
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .seat svg {
    flex: none;
    color: var(--text-3);
  }
  .name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    font-size: var(--fs-body);
    line-height: 22px;
    text-align: left;
  }
  .and {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .note {
    margin-top: 20px;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .note .why {
    margin: 0;
  }
  .chips {
    margin: 10px calc(-1 * var(--gutter)) -6px;
    padding: 6px 0 6px var(--gutter);
    display: flex;
    gap: 8px;
    overflow-x: auto;
  }
  .chips::after {
    content: '';
    flex: none;
    width: 8px;
  }
  .dot {
    flex: none;
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
  }
  .order {
    margin-top: 12px;
  }
  .order > button {
    white-space: nowrap;
  }
  .legend {
    margin-top: 20px;
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
  .group .list-header {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .more {
    width: 100%;
    margin-top: 24px;
    min-height: 48px;
  }
  .bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 44px;
  }
  .bar .btn-plain {
    margin-right: -8px;
    font-weight: 600;
  }
  .picker {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .picker .segmented > button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-width: 0;
  }
  .who {
    min-height: 64px;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .dim {
    opacity: 0.45;
  }
  .check {
    flex: none;
    color: var(--text);
  }
</style>
