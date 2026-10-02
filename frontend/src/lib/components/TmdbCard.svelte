<script>
  // The short card (decision 558): poster, name, year, genres, overview, Want it and View on TMDB, with no
  // taste, ranking or seen rows. `hit` is a From TMDB row in a sheet of its own; `title` is the card
  // payload of a title minted for a wish and not yet owned, drawn as the `body` of TitleDetail's sheet.
  import { tmdbKey, tmdbWants, toggleTmdbWant, wantError } from '$lib/beyond.svelte.js';
  import { genreLine } from '$lib/titleCard.js';
  import { clearWish, setWish } from '$lib/wish.svelte.js';
  import Icon from './Icon.svelte';
  import RatePoster from './RatePoster.svelte';
  import Sheet from './Sheet.svelte';

  let { hit = null, title = null, body = false, onClose = undefined, onOpenTitle = undefined } = $props();

  let open = $state(true);
  let busy = $state(false);
  let note = $state('');
  // Run once the sheet has closed, so its history entry is gone before the title's own card opens.
  let afterClose = null;

  const t = $derived(hit ?? title?.title ?? {});
  const noun = $derived(t.kind === 'series' ? 'series' : 'film');
  const genres = $derived(genreLine(hit ? hit.genres : title?.genres));
  const link = $derived(
    hit?.link ?? (t.tmdb_id ? `https://www.themoviedb.org/${t.kind === 'series' ? 'tv' : 'movie'}/${t.tmdb_id}` : null)
  );
  // The minted title's own want, from its card until Want it moves it.
  let titleWish = $derived(title?.wish?.state ?? null);
  const wanted = $derived(hit ? tmdbWants[tmdbKey(hit)]?.state === 'want' : titleWish === 'want');

  async function want(close) {
    if (busy) return;
    busy = true;
    note = '';
    try {
      if (hit) {
        const res = await toggleTmdbWant(hit);
        if (res.owned) {
          const seed = { id: res.title_id, kind: t.kind, name: t.name, year: t.year };
          const openTitle = onOpenTitle;
          afterClose = () => openTitle?.(seed);
          close();
        }
      } else {
        const res = titleWish === 'want' ? await clearWish(t.id) : await setWish(t.id, 'want');
        titleWish = res?.state ?? null;
      }
    } catch (err) {
      note = wantError(err);
    } finally {
      busy = false;
    }
  }

  function closed() {
    open = false;
    const run = afterClose;
    afterClose = null;
    onClose?.();
    run?.();
  }
</script>

{#snippet glyph(name, size)}
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width={name === 'close' ? 2.25 : 1.75} stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {#if name === 'globe'}
      <circle cx="12" cy="12" r="8.5" /><path d="M3.5 12h17M12 3.5c2.3 2.4 3.5 5.2 3.5 8.5s-1.2 6.1-3.5 8.5c-2.3-2.4-3.5-5.2-3.5-8.5s1.2-6.1 3.5-8.5z" />
    {:else if name === 'out'}
      <path d="M9 5.5h9.5V15" /><path d="M18.5 5.5 6 18" />
    {:else if name === 'close'}
      <path d="M6 6l12 12M18 6 6 18" />
    {/if}
  </svg>
{/snippet}

{#snippet card(close)}
  <div class="short" data-testid="tmdb-card">
    <div class="art"><RatePoster title={hit ?? t} showName={false} /></div>
    <div class="head">
      <h2 class="title-1">{t.name}</h2>
      {#if t.year}<p class="sub">{t.year}</p>{/if}
      {#if genres}<p class="sub" data-testid="tmdb-card-genres">{genres}</p>{/if}
    </div>
    <div class="main">
      <div class="unknown">
        <div class="unknown-head">
          {@render glyph('globe', 20)}
          <div>
            <p>Not in Spielplan yet</p>
            <p class="why">
              Found on TMDB, so there's no read on it for you yet. Once it's in the library, Spielplan
              learns it like any other {noun}.
            </p>
          </div>
        </div>
        <button
          class={wanted ? 'btn-tinted' : 'btn-primary'}
          aria-pressed={wanted}
          aria-busy={busy}
          onclick={() => want(close)}
          data-testid="tmdb-want"
        >
          <Icon name={wanted ? 'bookmark-fill' : 'bookmark'} size={20} />{wanted ? 'On the wish list' : 'Want it'}
        </button>
        {#if note}<p class="footnote" role="status">{note}</p>{/if}
      </div>
      {#if t.overview}<p class="overview">{t.overview}</p>{/if}
      <div class="source">
        <p class="footnote">Details from TMDB</p>
        {#if link}
          <a class="out" href={link} target="_blank" rel="noreferrer" data-testid="tmdb-link"
            >View on TMDB{@render glyph('out', 16)}</a
          >
        {/if}
      </div>
    </div>
  </div>
{/snippet}

{#if body}
  {@render card(() => {})}
{:else}
  <Sheet {open} onClose={closed} label={t.name} width={760}>
    {#snippet children(close)}
      <button class="close" onclick={close} aria-label="Close">
        <span class="disc">{@render glyph('close', 14)}</span>
      </button>
      {@render card(close)}
    {/snippet}
  </Sheet>
{/if}

<style>
  p,
  h2 {
    margin: 0;
  }
  .close {
    position: absolute;
    top: 4px;
    right: 4px;
    z-index: 3;
    width: var(--touch);
    height: var(--touch);
    padding: 0;
    border: none;
    background: none;
    display: grid;
    place-items: center;
  }
  .disc {
    width: 30px;
    height: 30px;
    border-radius: var(--r-pill);
    background: rgba(245, 240, 232, 0.12);
    color: var(--text-2);
    display: grid;
    place-items: center;
  }
  .short {
    display: grid;
    grid-template-columns: 120px minmax(0, 1fr);
    grid-template-areas: 'art head' 'main main';
    gap: 24px 16px;
    align-items: end;
    padding-top: 8px;
  }
  .art {
    grid-area: art;
  }
  .head {
    grid-area: head;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding-right: 24px;
  }
  .sub {
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .main {
    grid-area: main;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .unknown {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 14px var(--card-pad);
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .unknown-head {
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }
  .unknown-head > svg {
    flex: none;
    margin-top: 1px;
    color: var(--text-2);
  }
  .unknown-head > div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .unknown-head p:first-child {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .unknown .why {
    text-wrap: pretty;
  }
  .unknown > button {
    width: 100%;
    min-height: var(--touch);
  }
  .unknown > button[aria-busy='true'] {
    opacity: 0.6;
    transition-delay: 120ms;
  }
  .overview {
    font-size: var(--fs-subhead);
    line-height: 21px;
    color: var(--text-2);
    text-wrap: pretty;
  }
  .source {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .out {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    min-height: 44px;
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    line-height: 20px;
    text-decoration: none;
  }

  /* A desktop sheet is a centred panel: the poster beside everything the phone stacks under it. */
  @media (min-width: 721px) {
    .short {
      grid-template-columns: 220px minmax(0, 1fr);
      grid-template-areas: 'art head' 'art main';
      grid-template-rows: auto 1fr;
      gap: 16px 32px;
      align-items: start;
      padding-top: 16px;
    }
    .head {
      padding-right: 40px;
    }
    .head .title-1 {
      font-size: var(--fs-display);
      line-height: 48px;
    }
    .main {
      gap: 16px;
    }
    .unknown > button {
      min-height: 50px;
    }
  }
</style>
