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

  let { title, onSelect } = $props();

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
      <!-- The shelf or grid carries the explaining sentence once; the tooltip is for a pointer. -->
      <span class="new" data-testid="new-badge" title="No outside ratings yet — placed by what it's about"
        >New</span
      >
    {/if}
    {#if title.seen_state === 'seen'}
      <span class="seen" role="img" aria-label="Seen" data-testid="seen-badge">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
      </span>
    {/if}
    {#if title.is_owned === true}
      <span class="owned" data-testid="owned-chip">In library</span>
    {/if}
  </div>
  <span class="meta">
    <span class="name">{names.primary}</span>
    <span class="sub">{meta}</span>
  </span>
</button>

<style>
  .card-wrap {
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: 100%;
    min-width: 0;
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    text-align: left;
    color: inherit;
  }
  .poster {
    transition: filter 0.12s var(--ease);
  }
  .card-wrap:hover .poster {
    filter: brightness(1.08);
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
  .new,
  .owned {
    position: absolute;
    left: 6px;
    height: 20px;
    padding: 0 7px;
    border-radius: var(--r-xs);
    display: grid;
    place-items: center;
    font-size: var(--fs-caption);
    line-height: 16px;
    font-weight: 600;
  }
  .new {
    top: 6px;
    background: var(--text);
    color: var(--bg);
  }
  .owned {
    bottom: 6px;
    background: rgba(12, 11, 10, 0.72);
    color: var(--text);
  }
  .seen {
    position: absolute;
    top: 6px;
    right: 6px;
    width: 22px;
    height: 22px;
    border-radius: var(--r-pill);
    background: rgba(12, 11, 10, 0.72);
    color: var(--text);
    display: grid;
    place-items: center;
  }
  .meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .name {
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 500;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
</style>
