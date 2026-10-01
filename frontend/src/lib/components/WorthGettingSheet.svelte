<script>
  // Worth getting's See all: the whole list for one member or for everyone (decision 552), each row
  // with Want. A row's like line is always one of the viewer's own films.
  import Avatar from '$lib/components/Avatar.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import { toPosterTitle } from '$lib/home.svelte.js';
  import { runtimeLabel } from '$lib/rate.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import {
    clearWish,
    likeLine,
    loadWorthGetting,
    peopleLine,
    setWish,
    wishes,
    worthLede
  } from '$lib/wish.svelte.js';

  let { open = false, onClose, kind = 'movie', onSelect } = $props();

  // Whom the list is for: null is the viewer, else another member's id or 'everyone'.
  let audience = $state(null);
  let picking = $state(false);
  let data = $state(null);
  let failure = $state('');
  let busy = $state(null);

  const viewer = $derived(session.user?.id ?? null);
  const members = $derived(data?.members ?? []);
  const pickable = $derived(members.filter((m) => m.pickable));
  const everyone = $derived(audience === 'everyone');
  const chosen = $derived(
    everyone ? null : (members.find((m) => m.id === (audience ?? viewer)) ?? session.user)
  );
  const forWhom = $derived(everyone ? 'everyone' : chosen);
  const forName = $derived(everyone ? 'everyone' : chosen?.id === viewer ? 'you' : chosen?.name);
  const yours = $derived(everyone ? pickable.some((m) => m.id === viewer) : audience === null);
  // The list on screen answers another choice until the new one lands.
  const showing = $derived(
    data?.for === 'everyone' ? 'everyone' : data?.for?.id === viewer ? null : (data?.for?.id ?? null)
  );
  const stale = $derived(!!data && showing !== audience);

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

  // Opening it, choosing whom it is for, or a wish written from a card over it reads the list.
  $effect(() => {
    void wishes.epoch;
    if (open) load(kind, audience);
  });

  function choose(next, close) {
    close();
    audience = next;
  }

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

{#snippet all()}
  <span class="all" aria-hidden="true"><Icon name="people" size={18} /></span>
{/snippet}

{#snippet check()}
  <svg class="check" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
{/snippet}

<Sheet {open} {onClose} label="Worth getting" width={600}>
  {#snippet header(close)}
    <div class="sheet-bar">
      <button class="btn-plain done" onclick={close}>Done</button>
    </div>
    <h2 class="title-1">Worth getting</h2>
    <p class="why lede">{worthLede(kind, forWhom, viewer)}</p>
    <button
      class="seat"
      aria-haspopup="dialog"
      aria-expanded={picking}
      aria-label="For {forName}. Change"
      data-testid="worth-getting-for"
      onclick={() => (picking = true)}
    >
      {#if everyone}{@render all()}{:else}<Avatar name={chosen?.name} person={chosen} />{/if}
      <span class="name">For {forName}</span>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5.5 9.5 6.5 6.5 6.5-6.5" /></svg>
    </button>
  {/snippet}
  <div class="list" data-testid="worth-getting-sheet" data-for={audience ?? 'you'}>
    {#if failure}<p class="footnote" role="alert">{failure}</p>{/if}
    {#if data && !stale && !data.items.length && !failure}
      <p class="why empty">Nothing to suggest here yet.</p>
    {/if}
    <ul class:stale aria-busy={stale}>
      {#each data?.items ?? [] as item (item.title_id)}
        <li>
          <button class="open" onclick={() => onSelect?.(toPosterTitle(item))}>
            <span class="thumb"><RatePoster title={item} showName="missing" lazy /></span>
            <span class="text">
              <span class="name">{item.name}</span>
              <span class="footnote meta">{meta(item)}</span>
              {#if likeLine(item)}<span class="reason">{likeLine(item)}</span>{/if}
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
    {#if yours}
      <p class="footnote">Seen one already? Open it and rate it: it leaves this list and sharpens the rest.</p>
    {/if}
  </div>
</Sheet>

<Sheet open={picking} onClose={() => (picking = false)} label="Worth getting for" detent="fit" width={480}>
  {#snippet children(close)}
    <div class="bar">
      <h2 class="section-title">Worth getting for</h2>
      <button class="btn-plain" onclick={close}>Done</button>
    </div>
    <div class="list-group" role="radiogroup" aria-label="Worth getting for" data-testid="worth-getting-picker">
      {#each members as m (m.id)}
        {@const mine = !everyone && (audience ?? viewer) === m.id}
        <button
          class="list-row who"
          role="radio"
          aria-checked={mine}
          aria-disabled={!m.pickable}
          onclick={() => m.pickable && choose(m.id === viewer ? null : m.id, close)}
        >
          <span class:dim={!m.pickable}><Avatar name={m.name} person={m} /></span>
          <span class="text">
            <span class="name" class:dim={!m.pickable}>{m.id === viewer ? 'You' : m.name}</span>
            {#if m.reason}<span class="footnote">{m.reason}</span>{/if}
          </span>
          {#if mine}{@render check()}{/if}
        </button>
      {/each}
      <button
        class="list-row who"
        role="radio"
        aria-checked={everyone}
        aria-disabled={!pickable.length}
        onclick={() => pickable.length && choose('everyone', close)}
      >
        <span class:dim={!pickable.length}>{@render all()}</span>
        <span class="text">
          <span class="name" class:dim={!pickable.length}>Everyone</span>
          <span class="footnote">{pickable.length ? peopleLine(pickable, viewer) : 'No one has rated enough yet'}</span>
        </span>
        {#if everyone}{@render check()}{/if}
      </button>
    </div>
    <p class="list-footer">
      Everyone ranks what would suit all of you together. Whoever the list is for, each film is matched
      to one you liked.
    </p>
  {/snippet}
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
  .seat {
    width: 100%;
    height: 52px;
    margin: 14px 0 6px;
    padding: 0 12px;
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
  .seat .name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    font-size: var(--fs-body);
    line-height: 22px;
    text-align: left;
  }
  .all {
    flex: none;
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    color: var(--text-2);
  }
  .list {
    padding-top: 4px;
  }
  .empty {
    margin: 12px 0 0;
  }
  ul {
    margin: 0;
    padding: 0;
    list-style: none;
    transition: opacity var(--dur-base) var(--ease);
  }
  ul.stale {
    opacity: 0.4;
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
  .thumb :global(.poster .name) {
    inset: auto 5px 5px;
    font-family: var(--serif);
    font-weight: 400;
    line-height: 14px;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .text > .name {
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
  .bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 44px;
    margin-bottom: 12px;
  }
  .bar .btn-plain {
    margin-right: -8px;
    font-weight: 600;
  }
  .who {
    min-height: 64px;
  }
  .who .text > .name {
    font-weight: 400;
  }
  .dim {
    opacity: 0.45;
  }
  .check {
    flex: none;
    margin-left: auto;
    color: var(--text);
  }

  @media (min-width: 721px) {
    .seat {
      max-width: 320px;
    }
  }
</style>
