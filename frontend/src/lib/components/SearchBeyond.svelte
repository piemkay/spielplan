<script>
  // Under Home's own grid while Only in library is off (decision 558): More in Spielplan, the
  // catalogue's titles not owned, and From TMDB, the titles Spielplan does not hold. `params` are the
  // grid's own filters (`catalogParams()`); `onSelect` opens a Spielplan title's card.
  import { get } from '$lib/api.js';
  import {
    TMDB_WAIT_MS,
    kindNoun,
    moreQuery,
    tmdbKey,
    tmdbQuery,
    tmdbWants,
    toggleTmdbWant,
    wantError
  } from '$lib/beyond.svelte.js';
  import { showToast } from '$lib/toast.svelte.js';
  import Icon from './Icon.svelte';
  import PosterCard from './PosterCard.svelte';
  import RatePoster from './RatePoster.svelte';
  import TmdbCard from './TmdbCard.svelte';

  let { kinds, q, params, onSelect } = $props();

  // The grid's own debounce, so a word typed costs one read here too.
  const CATALOGUE_WAIT_MS = 220;
  // TMDB's tail is its least known; the rest waits behind one button.
  const TMDB_FIRST = 8;

  // `query` is the read the listed titles answer; null before the first one lands.
  let more = $state({ query: null, items: [], total: 0, loading: false, error: '' });
  let moreSeq = 0;
  const moreAsk = $derived(moreQuery(kinds, params));

  $effect(() => {
    const query = moreAsk;
    const seq = ++moreSeq;
    const timer = setTimeout(() => loadMore(seq, query), CATALOGUE_WAIT_MS);
    return () => clearTimeout(timer);
  });

  async function loadMore(seq, query, append = false) {
    more.loading = true;
    try {
      const res = await get(append ? moreQuery(kinds, params, { offset: more.items.length }) : query);
      if (seq !== moreSeq) return;
      more = {
        query,
        items: append ? [...more.items, ...res.items] : res.items,
        total: res.total,
        loading: false,
        error: ''
      };
    } catch (err) {
      if (seq === moreSeq) more = { ...more, query, loading: false, error: err.message };
    }
  }

  // `path` is the read the hits answer; the section stands only on an answer that says available.
  let tmdb = $state({ path: null, available: false, items: [] });
  let tmdbSeq = 0;
  let allHits = $state(false);
  const tmdbAsk = $derived(tmdbQuery(kinds, q));
  const hits = $derived(allHits ? tmdb.items : tmdb.items.slice(0, TMDB_FIRST));

  $effect(() => {
    const path = tmdbAsk;
    const seq = ++tmdbSeq;
    if (!path) {
      tmdb = { path: null, available: false, items: [] };
      return;
    }
    const timer = setTimeout(async () => {
      // A failed read is no answer, and TMDB unreachable means the section is absent.
      const res = await get(path).catch(() => null);
      if (seq !== tmdbSeq) return;
      tmdb = { path, available: !!res?.available, items: res?.available ? res.items : [] };
      allHits = false;
    }, TMDB_WAIT_MS);
    return () => clearTimeout(timer);
  });

  let opened = $state(null);
  let wanting = $state(null);

  const seedOf = (hit, id) => ({ id, kind: hit.kind, name: hit.name, year: hit.year });

  async function want(hit) {
    if (wanting) return;
    wanting = tmdbKey(hit);
    try {
      const res = await toggleTmdbWant(hit);
      if (res.owned) onSelect(seedOf(hit, res.title_id));
    } catch (err) {
      showToast(wantError(err));
    } finally {
      wanting = null;
    }
  }
</script>

<div class="beyond" data-testid="beyond-sections">
  <section class="part" data-testid="beyond-catalogue">
    <header class="head">
      <h2 class="section-title">More in Spielplan</h2>
      <p class="why">Not in your library yet</p>
    </header>
    {#if more.error}
      <p class="why">{more.error}</p>
    {:else if more.query === null}
      <div class="grid" aria-hidden="true">
        {#each { length: 3 }, i (i)}<span class="skeleton ghost"></span>{/each}
      </div>
    {:else if !more.items.length}
      <p class="footnote" data-testid="beyond-catalogue-empty">Nothing else in Spielplan matches.</p>
    {:else}
      <div class="dims" aria-busy={more.query !== moreAsk}>
        <div class="grid">
          {#each more.items as t (t.id)}
            <PosterCard title={t} onSelect={() => onSelect(t)} />
          {/each}
        </div>
        {#if more.items.length < more.total}
          <div class="more">
            <button
              class="btn-secondary"
              data-testid="beyond-catalogue-more"
              disabled={more.loading || more.query !== moreAsk}
              onclick={() => loadMore(moreSeq, more.query, true)}
            >
              {more.loading ? 'Loading…' : `Show ${(more.total - more.items.length).toLocaleString()} more`}
            </button>
          </div>
        {/if}
      </div>
    {/if}
  </section>

  {#if tmdb.available}
    <section class="part" data-testid="beyond-tmdb">
      <header class="head">
        <h2 class="section-title">From TMDB</h2>
        <p class="why">Spielplan doesn't know these {kindNoun(kinds)} yet</p>
      </header>
      {#if !tmdb.items.length}
        <p class="footnote">Nothing else on TMDB matches.</p>
      {:else}
        <ul class="hits dims" aria-busy={tmdb.path !== tmdbAsk}>
          {#each hits as hit (tmdbKey(hit))}
            {@const wanted = tmdbWants[tmdbKey(hit)]?.state === 'want'}
            <li class="tmdb-row" data-testid="tmdb-hit">
              <button class="open" aria-haspopup="dialog" onclick={() => (opened = hit)}>
                <span class="thumb"><RatePoster title={hit} showName={false} lazy /></span>
                <span class="text">
                  <span class="name">{hit.name}</span>
                  <span class="meta">{[hit.year, 'TMDB'].filter(Boolean).join(' · ')}</span>
                </span>
              </button>
              <button
                class="pill want"
                aria-pressed={wanted}
                aria-busy={wanting === tmdbKey(hit)}
                aria-label="Want {hit.name}"
                onclick={() => want(hit)}
                data-testid="tmdb-hit-want"
              >
                <Icon name={wanted ? 'bookmark-fill' : 'bookmark'} size={16} />
                {wanted ? 'On the wish list' : 'Want it'}
              </button>
            </li>
          {/each}
        </ul>
        {#if hits.length < tmdb.items.length}
          <button class="btn-plain" onclick={() => (allHits = true)}>
            Show {tmdb.items.length - hits.length} more from TMDB
          </button>
        {/if}
      {/if}
    </section>
  {/if}
</div>

{#if opened}
  <TmdbCard
    hit={opened}
    onClose={() => (opened = null)}
    onOpenTitle={(seed) => onSelect(seed)}
  />
{/if}

<style>
  .beyond {
    display: flex;
    flex-direction: column;
    gap: 32px;
    margin-top: 32px;
  }
  .part {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .head {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .head p {
    margin: 0;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 20px 12px;
  }
  .ghost {
    display: block;
    aspect-ratio: 2 / 3;
  }
  .more {
    display: flex;
    padding-top: 24px;
  }

  /* One grouped list on a phone; from 1024 px, cards two to a line. */
  .hits {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    border-radius: var(--r-md);
    background: var(--surface-1);
    overflow: hidden;
  }
  .tmdb-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding-right: 12px;
  }
  .tmdb-row + .tmdb-row {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .open {
    flex: 1;
    min-width: 0;
    min-height: 85px;
    padding: 8px 0 8px 12px;
    display: flex;
    align-items: center;
    gap: 12px;
    border: none;
    background: none;
    color: inherit;
    text-align: left;
    cursor: pointer;
  }
  .thumb {
    flex: none;
    width: 46px;
  }
  .thumb :global(.poster) {
    border-radius: var(--r-xs);
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .meta {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .want {
    flex: none;
    padding: 0 14px 0 12px;
    font-weight: 600;
  }
  .want[aria-pressed='true'] {
    background: var(--accent-tint);
    color: var(--accent-text);
  }
  .want[aria-busy='true'] {
    opacity: 0.6;
    transition-delay: 120ms;
  }
  .btn-plain {
    align-self: flex-start;
    padding: 0;
  }

  @media (min-width: 721px) {
    .grid {
      grid-template-columns: repeat(auto-fill, var(--shelf-poster));
      justify-content: space-between;
      gap: 24px 16px;
    }
  }
  @media (min-width: 1024px) {
    .hits {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      border-radius: 0;
      background: none;
      overflow: visible;
    }
    .tmdb-row {
      border-radius: var(--r-md);
      background: var(--surface-1);
    }
    .tmdb-row + .tmdb-row {
      box-shadow: none;
    }
  }
</style>
