<script module>
  // One copy of §8 stage 10's test, for the card's chip and the shelf's why-line. A title with
  // crowd ratings is never "new": the evaluation holdout serves such rows from the Cold Tower.
  export function isColdPlaced(title) {
    return !(title.item_n > 0) && (title.e_source
      ? title.e_source === 'cold_tower'
      : title.item_n === 0 || (title.item_n == null && title.placement === 'cold_tower'));
  }
</script>

<script>
  import { runtimeLabel } from '$lib/rate.svelte.js';
  import { noteMissing, posterSrc } from '$lib/art.js';
  import { displayNames } from '$lib/titleCard.js';

  // `chrome` is a row the caller draws between the art and the name, never over the art.
  let { title, onSelect, chrome = null } = $props();

  const names = $derived(displayNames(title));

  // FNV-1a over the name: the same film is the same colour everywhere.
  function hue(text) {
    let x = 2166136261;
    for (let i = 0; i < text.length; i++) {
      x ^= text.charCodeAt(i);
      x = Math.imul(x, 16777619);
    }
    return (x >>> 0) % 360;
  }

  const h = $derived(hue(title.name ?? String(title.id)));
  const minutes = $derived(runtimeLabel(title));
  // Built in JS: Svelte collapses the whitespace around an {#if}, gluing the separator.
  const meta = $derived([title.year ?? '—', minutes].filter(Boolean).join(' · '));
  const noCrowdData = $derived(isColdPlaced(title));
  // The src that failed, not a flag, so a reused card does not inherit a missing image.
  const src = $derived(posterSrc(title));
  let failed = $state(null);
</script>

<button
  class="card-wrap"
  onclick={onSelect}
  title={names.secondary ? `${names.primary} (${names.secondary})` : names.primary}
>
  <div
    class="poster"
    style:background="linear-gradient(150deg, hsl({h} 22% 17%), hsl({(h + 40) % 360} 18% 11%))"
  >
    {#if src && failed !== src}
      <!-- alt="": the name is printed under the poster. Lazy: a grid draws more cards than fit. -->
      <img
        class="art"
        {src}
        alt=""
        loading="lazy"
        decoding="async"
        draggable="false"
        onerror={() => {
          failed = src;
          noteMissing(src);
        }}
      />
    {/if}
    {#if noCrowdData}
      <!-- The shelf or grid header carries the explaining sentence once; the tooltip is for a pointer. -->
      <span class="badge data" title="no outside ratings yet — placed by what it's about">new</span>
    {/if}
    {#if title.seen_state === 'seen'}
      <span class="seen data">seen</span>
    {/if}
    {#if title.is_owned === true}
      <span class="owned data" data-testid="owned-chip">in library</span>
    {/if}
  </div>
  {#if chrome}
    <div class="chrome">{@render chrome()}</div>
  {/if}
  <div class="meta">
    <div class="name">{names.primary}</div>
    <div class="data">{meta}</div>
  </div>
</button>

<style>
  .card-wrap {
    display: flex;
    flex-direction: column;
    gap: 7px;
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    text-align: left;
    color: inherit;
  }
  .card-wrap:hover .poster {
    border-color: var(--ember-edge);
  }
  .poster {
    transition: border-color 0.12s ease;
  }
  /* Inert, so the card's button takes every tap and a long press offers no image callout. */
  .art {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    pointer-events: none;
    user-select: none;
    -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  .badge,
  .seen {
    position: absolute;
    top: 7px;
    padding: 2px 7px;
    border-radius: var(--r-pill);
    font-size: 9px;
    letter-spacing: 0.06em;
  }
  /* A status, not the accent (§6.8); opaque, because an alpha fill over art varies in contrast. */
  .badge {
    left: 7px;
    background: var(--status);
    color: var(--ground);
  }
  .seen {
    right: 7px;
    background: rgba(13, 13, 15, 0.72);
    color: var(--ink-3);
  }
  .owned {
    position: absolute;
    bottom: 7px;
    left: 7px;
    padding: 2px 7px;
    border-radius: var(--r-pill);
    letter-spacing: 0.06em;
    background: rgba(13, 13, 15, 0.72);
    border: 1px solid var(--line-2);
    color: var(--ink-2);
  }
  .chrome {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    min-height: 18px;
    margin-top: -2px;
  }
  .name {
    font-size: 12.5px;
    line-height: 1.25;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
</style>
