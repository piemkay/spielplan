<script>
  // Worth getting's See all: the whole list, For you or For you and {other}, each row with Want.
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import { toPosterTitle } from '$lib/home.svelte.js';
  import { runtimeLabel } from '$lib/rate.svelte.js';
  import { clearWish, likeLine, loadWorthGetting, setWish } from '$lib/wish.svelte.js';

  let { open = false, onClose, kind = 'movie', onSelect } = $props();

  let audience = $state('me');
  let data = $state(null);
  let failure = $state('');
  let busy = $state(null);

  const noun = $derived(kind === 'series' ? 'Series' : 'Films');
  const otherName = $derived(audience === 'pair' ? (data?.other?.name ?? '') : '');

  let seq = 0;
  async function load(forKind, forAudience) {
    const mine = ++seq;
    failure = '';
    try {
      const res = await loadWorthGetting(forKind, forAudience);
      if (mine === seq) data = res;
    } catch (err) {
      if (mine === seq) failure = err.message;
    }
  }

  // Opening the sheet or switching whose list it is reads that list.
  $effect(() => {
    if (open) load(kind, audience);
  });

  async function toggle(item) {
    if (busy) return;
    busy = item.title_id;
    failure = '';
    try {
      if (item.wanted) await clearWish(item.title_id);
      else await setWish(item.title_id, 'want');
      item.wanted = !item.wanted;
    } catch (err) {
      failure = err.message;
    } finally {
      busy = null;
    }
  }

  function meta(item) {
    return [item.year, runtimeLabel(item)].filter(Boolean).join(' · ');
  }
</script>

<Sheet {open} {onClose} label="Worth getting">
  {#snippet header(close)}
    <div class="sheet-bar">
      <button class="btn-plain done" onclick={close}>Done</button>
    </div>
    <h2 class="title-1">Worth getting</h2>
    <p class="why lede">{noun} the library doesn't have, closest to the ones you rate highest.</p>
    {#if data?.other}
      <div class="segmented whose" role="group" aria-label="Whose list">
        <button aria-pressed={audience === 'me'} onclick={() => (audience = 'me')}>For you</button>
        <button aria-pressed={audience === 'pair'} onclick={() => (audience = 'pair')}
          >For you and {data.other.name}</button
        >
      </div>
    {/if}
  {/snippet}
  <div class="list" data-testid="worth-getting-sheet" data-with={audience}>
    {#if failure}<p class="footnote" role="alert">{failure}</p>{/if}
    <ul>
      {#each data?.items ?? [] as item (item.title_id)}
        <li>
          <button class="open" onclick={() => onSelect?.(toPosterTitle(item))}>
            <span class="thumb"><RatePoster title={item} showName={false} lazy /></span>
            <span class="text">
              <span class="name">{item.name}</span>
              <span class="footnote meta">{meta(item)}</span>
              {#if likeLine(item, otherName)}<span class="reason">{likeLine(item, otherName)}</span>{/if}
            </span>
          </button>
          <button
            class="want"
            class:on={item.wanted}
            aria-pressed={item.wanted}
            aria-label="{item.wanted ? 'Wanted' : 'Want'}: {item.name}"
            data-testid="worth-getting-want"
            disabled={busy === item.title_id}
            onclick={() => toggle(item)}
          >
            <Icon name={item.wanted ? 'bookmark-fill' : 'bookmark'} size={20} />
            {item.wanted ? 'Wanted' : 'Want'}
          </button>
        </li>
      {/each}
    </ul>
    <p class="footnote">Seen one already? Open it and rate it: it leaves this list and sharpens the rest.</p>
  </div>
</Sheet>

<style>
  .sheet-bar {
    display: flex;
    justify-content: flex-end;
    margin: -4px -8px 0;
  }
  .done {
    min-height: 44px;
    font-weight: 600;
  }
  .lede {
    margin: 4px 0 0;
  }
  .whose {
    margin: 14px 0 6px;
    min-height: 32px;
  }
  .whose > button {
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .list {
    padding-top: 4px;
  }
  ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  li {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 0;
    box-shadow: inset 0 -0.5px 0 var(--separator);
  }
  .open {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0;
    border: none;
    background: none;
    text-align: left;
  }
  .thumb {
    flex: none;
    width: 56px;
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
  }
  .meta {
    font-variant-numeric: tabular-nums;
  }
  .reason {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .want {
    flex: none;
    width: 56px;
    min-height: 52px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    border: none;
    border-radius: 12px;
    background: var(--surface-2);
    color: var(--text-2);
    font-size: 11px;
    line-height: 13px;
    font-weight: 500;
  }
  .want.on {
    background: var(--accent-tint);
    color: var(--accent-text);
    font-weight: 600;
  }
  .list > .footnote {
    margin: 14px 0 0;
  }
</style>
