<script>
  /**
   * Poster-forward 2:3 card (spec §6.8). There are no poster images in the corpus bundle —
   * only paths — so until an image source is configured the card renders a deterministic
   * tinted panel derived from the title's own id. It is a placeholder that is stable across
   * reloads, which matters: a card that changes colour every render reads as a bug.
   */
  let { title, onSelect } = $props();

  // A stable hue per title. FNV-1a over the name, so the same film is the same colour
  // everywhere it appears.
  function hue(text) {
    let x = 2166136261;
    for (let i = 0; i < text.length; i++) {
      x ^= text.charCodeAt(i);
      x = Math.imul(x, 16777619);
    }
    return (x >>> 0) % 360;
  }

  const h = $derived(hue(title.name ?? String(title.id)));
  const minutes = $derived(
    title.runtime_min
      ? title.kind === 'series'
        ? `${title.runtime_min}m/ep`
        : `${Math.floor(title.runtime_min / 60)}h ${title.runtime_min % 60}m`
      : null
  );
  // Built in JS, not in markup: Svelte collapses the whitespace around an {#if} block, which
  // turned "1995 · 2h 50m" into "1995· 2h 50m".
  const meta = $derived([title.year ?? '—', minutes].filter(Boolean).join(' · '));
</script>

<button class="card-wrap" onclick={onSelect} title={title.name}>
  <div
    class="poster"
    style:background="linear-gradient(150deg, hsl({h} 22% 17%), hsl({(h + 40) % 360} 18% 11%))"
  >
    {#if title.placement === 'cold_tower'}
      <!-- §8 stage 10: "new — model placement, no crowd data" until ratings accrue. -->
      <span class="badge data" title="placed by the Cold Tower — no crowd data yet">new</span>
    {/if}
    {#if title.seen_state === 'seen'}
      <span class="seen data">seen</span>
    {/if}
  </div>
  <div class="meta">
    <div class="name">{title.name}</div>
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
    aspect-ratio: 2 / 3;
    border-radius: 10px;
    border: 1px solid var(--line);
    position: relative;
    overflow: hidden;
    transition: border-color 0.12s ease;
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
  .badge {
    left: 7px;
    background: var(--ember);
    color: var(--ember-ink);
  }
  .seen {
    right: 7px;
    background: rgba(13, 13, 15, 0.72);
    color: var(--ink-3);
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
