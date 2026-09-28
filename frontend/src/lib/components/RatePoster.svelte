<script>
  // Inert and badge-free: the poster is the button on the battle card, and the anchoring rule
  // forbids any model number before the answer (§6.1).
  import { hueOf } from '$lib/rate.svelte.js';
  import { noteMissing, posterSrc, titleIdOf } from '$lib/art.js';

  // `showName: 'missing'` names only a poster with no art; `lazy` suits a grid of many (Rank).
  /** @type {{ title: any, showName?: boolean | 'missing', lazy?: boolean }} */
  let { title, showName = true, lazy = false } = $props();

  const h = $derived(hueOf(title?.name ?? String(titleIdOf(title) ?? '')));
  const src = $derived(posterSrc(title));
  // The src that failed, rather than a flag: this component is reused as Rate's card changes,
  // and a flag would carry one title's missing art onto the next title's poster.
  let failed = $state(null);
</script>

<div
  class="poster"
  data-testid="rate-poster"
  data-title-id={titleIdOf(title)}
  style:background="linear-gradient(150deg, hsl({h} 22% 17%), hsl({(h + 40) % 360} 18% 11%))"
>
  <!-- Keyed on the URL: a reused <img> keeps the old picture until the new src loads. -->
  {#key src}
    {#if src && failed !== src}
      <!-- alt="": the title is printed on the card already. Eager: the next card was preloaded. -->
      <img
        class="art"
        {src}
        alt=""
        loading={lazy ? 'lazy' : 'eager'}
        decoding="async"
        draggable="false"
        onerror={() => {
          failed = src;
          noteMissing(src);
        }}
      />
    {/if}
  {/key}
  {#if showName === true || (showName === 'missing' && !(src && failed !== src))}
    <span class="scrim"></span>
    <span class="name">{title?.name ?? '—'}</span>
  {/if}
</div>

<style>
  .poster {
    display: block;
    width: 100%;
    transition: border-color 0.18s ease, transform 0.18s ease;
  }
  /* Inert: the poster sits inside a button, and iOS's image callout would take a long press. */
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
  .scrim {
    position: absolute;
    inset: auto 0 0 0;
    height: 46%;
    background: linear-gradient(to top, rgba(12, 11, 10, 0.88), rgba(12, 11, 10, 0));
  }
  .name {
    position: absolute;
    left: 10px;
    right: 10px;
    bottom: 9px;
    font-size: var(--fs-footnote);
    font-weight: 500;
    line-height: 18px;
    text-wrap: pretty;
  }
</style>
