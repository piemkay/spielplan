<script>
  // The household's wish list: grouped by who wants each title, with Me too, Remove and Copy the list.
  import Avatar from '$lib/components/Avatar.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import { session } from '$lib/session.svelte.js';
  import { showToast } from '$lib/toast.svelte.js';
  import {
    clearWish,
    dayMonth,
    groupHeading,
    loadWishList,
    othersLine,
    setWish,
    wishes
  } from '$lib/wish.svelte.js';

  let { open = false, onClose, onSelect } = $props();

  let data = $state(null);
  let failure = $state('');
  let busy = $state(null);
  let copyBlocked = $state(false);

  const viewer = $derived(session.user?.id ?? null);

  async function load() {
    failure = '';
    try {
      data = await loadWishList();
    } catch (err) {
      failure = err.message;
    }
  }

  // Every wish write bumps the epoch, this sheet's own and one from a card opened over it.
  $effect(() => {
    void wishes.epoch;
    if (open) load();
  });

  async function act(item, write) {
    if (busy) return;
    busy = item.title_id;
    failure = '';
    try {
      await write(item.title_id);
    } catch (err) {
      failure = err.message;
    } finally {
      busy = null;
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(data.copy_text);
      copyBlocked = false;
      showToast('Copied the list');
    } catch {
      copyBlocked = true;
    }
  }

  function meta(item) {
    return [item.year, item.since ? `since ${dayMonth(item.since)}` : '', othersLine(item.others_likely, viewer)]
      .filter(Boolean)
      .join(' · ');
  }
</script>

<Sheet {open} {onClose} label="Wish list">
  {#snippet header(close)}
    <div class="sheet-bar">
      <button class="btn-plain done" onclick={close}>Done</button>
    </div>
    <h2 class="title-1">Wish list</h2>
    <p class="why lede">What the household wants. A film leaves on its own once it turns up in Jellyfin.</p>
  {/snippet}
  <div class="body" data-testid="wish-list-sheet">
    {#if failure}<p class="footnote" role="alert">{failure}</p>{/if}
    {#if data && !data.groups.length}
      <p class="why">Nothing on the list yet. Want a film from Worth getting or from its card, and it shows here.</p>
    {/if}
    {#each data?.groups ?? [] as group (group.wanters.map((w) => w.id).join('-'))}
      <section class="group" data-testid="wish-group">
        <h3 class="list-header">{groupHeading(group.wanters, viewer)}</h3>
        <div class="list-group">
          {#each group.items as item (item.title_id)}
            <div class="row" data-testid="wish-item" data-title={item.title_id}>
              <button class="open" onclick={() => onSelect?.({ id: item.title_id, ...item })}>
                <span class="thumb"><RatePoster title={item} showName={false} lazy /></span>
                <span class="text">
                  <span class="name">{item.name}</span>
                  <span class="footnote">{meta(item)}</span>
                </span>
              </button>
              {#if group.wanters.length > 1}
                <span class="faces" role="img" aria-label={group.wanters.map((w) => w.name).join(' and ')}>
                  {#each group.wanters as person (person.id)}
                    <Avatar name={person.name} {person} size={24} />
                  {/each}
                </span>
              {/if}
              {#if item.mine}
                <button
                  class="remove"
                  aria-label="Remove {item.name}"
                  disabled={busy === item.title_id}
                  onclick={() => act(item, clearWish)}
                ><Icon name="close" size={18} /></button>
              {:else}
                <button
                  class="pill metoo"
                  disabled={busy === item.title_id}
                  onclick={() => act(item, (id) => setWish(id, 'want'))}
                >Me too</button>
              {/if}
            </div>
          {/each}
        </div>
      </section>
    {/each}
    {#if data?.groups.length}
      <button class="pill copy" data-testid="wish-copy" onclick={copy}><Icon name="copy" size={18} />Copy the list</button>
      <p class="footnote copynote">Titles, years and IMDb links, one per line, the most wanted first.</p>
      {#if copyBlocked}
        <p class="footnote" role="alert">This browser would not copy it. Here it is to select by hand:</p>
        <pre class="code">{data.copy_text}</pre>
      {/if}
    {/if}
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
  .body {
    display: flex;
    flex-direction: column;
    gap: 24px;
    padding-top: 20px;
  }
  .body > p {
    margin: 0;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .group .list-header {
    padding: 0 0 4px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 8px 10px 12px;
  }
  .row + .row {
    box-shadow: inset 0 0.5px 0 var(--separator);
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
    width: 44px;
  }
  .thumb :global(.poster) {
    border-radius: 5px;
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
  .faces {
    flex: none;
    display: flex;
  }
  .faces > :global(.avatar) {
    box-shadow: 0 0 0 2px var(--surface-1);
  }
  .faces > :global(.avatar + .avatar) {
    margin-left: -6px;
  }
  .remove {
    flex: none;
    width: 44px;
    height: 44px;
    border: none;
    background: none;
    color: var(--text-3);
    display: grid;
    place-items: center;
  }
  .metoo {
    flex: none;
    font-size: var(--fs-footnote);
    font-weight: 600;
  }
  .copy {
    align-self: flex-start;
    min-height: 44px;
    padding: 0 16px;
  }
  .body > .copynote {
    margin-top: -14px;
  }
  pre {
    margin: 0;
    white-space: pre-wrap;
    user-select: all;
  }
</style>
