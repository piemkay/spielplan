<script>
  /**
   * Home / Library. Spec v2.1 §6.0, M0 scope:
   *   "a paginated list over `title`, partitioned by kind (§4.1 rule 5), filter/search on
   *    title/alias/genre/decade/seen-state, and the title detail card".
   *
   * Home *shelves* are M2 (§12) and are deliberately not faked here — a shelf that cannot say
   * why it exists doesn't ship, and none of them can until the Ledger exists. Until then this
   * surface is the catalog, which is what search or an active person-filter shows anyway (§6.0).
   */
  import { onMount } from 'svelte';
  import { get, qs } from '$lib/api.js';
  import { session } from '$lib/session.svelte.js';
  import PosterCard from '$lib/components/PosterCard.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';

  // Owner decision 2026-08-29: kind is two independent toggles, either or both active,
  // never neither. A person filter no longer collides with it — turn both on and a
  // filmography is complete.
  let kinds = $state(['movie']);
  let q = $state('');
  let genre = $state('');
  let decade = $state('');
  let seen = $state('any');
  let personId = $state(null);
  let personName = $state('');

  let items = $state([]);
  let total = $state(0);
  let offset = $state(0);
  let loading = $state(false);
  let facets = $state({ genres: [], decades: [] });
  let hidden = $state({});
  let selected = $state(null);
  let loadError = $state('');

  const LIMIT = 60;
  const greeting = $derived(hourGreeting());
  const LABELS = { movie: 'film', series: 'series' };
  const plural = (kind, n) => (kind === 'movie' ? `film${n === 1 ? '' : 's'}` : 'series');

  const countLabel = $derived(
    [
      `${total.toLocaleString()} ${kinds.length === 1 ? plural(kinds[0], total) : 'titles'}`,
      // §6.0: a toggle that hides things has to say how many. Silent truncation is the
      // failure this control exists to fix.
      ...Object.entries(hidden).map(([k, n]) => `${n.toLocaleString()} ${plural(k, n)} hidden`)
    ].join(' · ')
  );

  function hourGreeting() {
    const h = new Date().getHours();
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  }

  // Filter changes fire overlapping requests; without a sequence number a slow earlier
  // response lands after a fast later one and the grid shows the wrong filter's results.
  let requestSeq = 0;

  async function load({ append = false } = {}) {
    const seq = ++requestSeq;
    loading = true;
    loadError = '';
    try {
      const res = await get(
        `/titles${qs({
          kind: kinds,
          q,
          genre,
          decade: decade || undefined,
          seen: seen === 'any' ? undefined : seen,
          person_id: personId ?? undefined,
          limit: LIMIT,
          offset: append ? offset : 0
        })}`
      );
      if (seq !== requestSeq) return;      // a newer request has already answered
      items = append ? [...items, ...res.items] : res.items;
      total = res.total;
      hidden = res.hidden ?? {};
      offset = (append ? offset : 0) + res.items.length;
    } catch (err) {
      if (seq === requestSeq) loadError = err.message;
    } finally {
      if (seq === requestSeq) loading = false;
    }
  }

  async function loadFacets() {
    facets =
      (await get(`/facets${qs({ kind: kinds })}`).catch(() => null)) ?? { genres: [], decades: [] };
  }

  onMount(async () => {
    await Promise.all([load(), loadFacets()]);
  });

  function toggleKind(k) {
    const on = kinds.includes(k);
    // Never neither: turning off the last active toggle would silently mean "everything",
    // which is the unpartitioned query §4.1 rule 5 exists to prevent.
    if (on && kinds.length === 1) return;
    kinds = on ? kinds.filter((x) => x !== k) : [...kinds, k];
    // The facet vocabulary is scoped to the selection, so a genre that only exists in the
    // kind you just switched off would otherwise stay selected and silently return nothing.
    genre = '';
    decade = '';
    loadFacets();
    load();
  }

  let debounce;
  function onQuery() {
    clearTimeout(debounce);
    debounce = setTimeout(() => load(), 220);
  }

  function filterToPerson(person) {
    personId = person.person_id ?? person.id;
    personName = person.name;
    selected = null;
    load();
  }

  function clearPerson() {
    personId = null;
    personName = '';
    load();
  }
</script>

<div class="head">
  <h1>{greeting}{session.user ? `, ${session.user.name}` : ''}</h1>

  <div class="controls">
    <div class="kinds" role="group" aria-label="Kind">
      <!-- Two toggles, either or both, never neither. §4.1 rule 5 partitions every *ranking*
           surface; this one only lists, in a kind-independent order, so it may interleave. -->
      <button
        class="pill"
        aria-pressed={kinds.includes('movie')}
        onclick={() => toggleKind('movie')}
        title={kinds.length === 1 && kinds[0] === 'movie' ? 'at least one kind stays on' : ''}
      >Films</button>
      <button
        class="pill"
        aria-pressed={kinds.includes('series')}
        onclick={() => toggleKind('series')}
        title={kinds.length === 1 && kinds[0] === 'series' ? 'at least one kind stays on' : ''}
      >Series</button>
    </div>

    <input
      type="search"
      bind:value={q}
      oninput={onQuery}
      placeholder="search title, alias"
      aria-label="Search titles"
    />
  </div>

  <div class="filters">
    <select bind:value={genre} onchange={() => load()} aria-label="Genre">
      <option value="">every genre</option>
      {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
    </select>
    <select bind:value={decade} onchange={() => load()} aria-label="Decade">
      <option value="">every decade</option>
      {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
    </select>
    <select bind:value={seen} onchange={() => load()} aria-label="Seen state">
      <option value="any">seen or not</option>
      <option value="seen">seen</option>
      <option value="unseen">unseen</option>
    </select>
    {#if personId}
      <button class="pill on" onclick={clearPerson}>{personName} ✕</button>
    {/if}
  </div>

  <div class="data count">
    {countLabel}{session.hasBundle ? '' : ' · no bundle imported'}
  </div>
</div>

{#if loadError}
  <div class="empty card">
    <p>{loadError}</p>
  </div>
{:else if !items.length && !loading}
  <div class="empty card">
    {#if !session.hasBundle}
      <h2>Nothing to show yet</h2>
      <p class="why">
        No artifact bundle has been imported. That is a legal state — the app runs, the setup
        wizard and admin routes work, and every artifact-dependent surface says so instead of
        erroring.
      </p>
      <a class="btn-primary" href="/admin/data">Import a bundle</a>
    {:else}
      <h2>No matches</h2>
      <p class="why">Nothing in the library fits those filters.</p>
    {/if}
  </div>
{:else}
  <div class="grid">
    {#each items as t (t.id)}
      <PosterCard title={t} onSelect={() => (selected = t.id)} />
    {/each}
  </div>
  {#if offset < total}
    <div class="more">
      <button class="btn-ghost" onclick={() => load({ append: true })} disabled={loading}>
        {loading ? 'Loading…' : `Show more · ${(total - offset).toLocaleString()} left`}
      </button>
    </div>
  {/if}
{/if}

{#if selected}
  <TitleDetail
    titleId={selected}
    onClose={() => (selected = null)}
    onPerson={filterToPerson}
  />
{/if}

<style>
  .head {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 18px;
  }
  h1 {
    margin: 0;
    font-size: 21px;
    font-weight: 600;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .kinds {
    display: flex;
    gap: 6px;
  }
  .controls input {
    flex: 1;
    min-width: 200px;
    max-width: 420px;
  }
  .filters {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  select {
    padding: 7px 10px;
    border-radius: var(--r-pill);
    border: 1px solid var(--line-2);
    background: var(--card);
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
  }
  .count {
    letter-spacing: 0.04em;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
    gap: 14px;
  }
  .more {
    display: flex;
    justify-content: center;
    padding: 22px 0;
  }
  .empty {
    padding: 30px;
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 10px;
    align-items: center;
  }
  .empty h2 {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
  }
  .empty .why {
    max-width: 46ch;
  }
  @media (max-width: 720px) {
    .grid {
      grid-template-columns: repeat(auto-fill, minmax(104px, 1fr));
      gap: 10px;
    }
  }
</style>
