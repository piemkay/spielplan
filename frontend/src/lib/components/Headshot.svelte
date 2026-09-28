<script>
  // A credit's face: its photo from this origin, else initials on the person's tone (decision 528).
  // The caller sizes it with `--face`.
  import { artReady, noteMissing, personSrc } from '$lib/art.js';
  import { avatarColour } from './Avatar.svelte';

  let { credit } = $props();

  const src = $derived(personSrc(credit));
  // The src that failed, not a flag, as RatePoster keeps it.
  let failed = $state(null);
  const photo = $derived(src !== null && failed !== src);
  const words = $derived(String(credit?.name ?? '').split(/\s+/).filter(Boolean));
  const initials = $derived(
    (words.length > 1 ? words[0].charAt(0) + words.at(-1).charAt(0) : (words[0] ?? '?').charAt(0)).toUpperCase()
  );
</script>

<span class="face" style:background={photo ? null : avatarColour({ id: credit?.person_id })} aria-hidden="true">
  {#if photo}
    <img
      {src}
      alt=""
      loading="lazy"
      decoding="async"
      {@attach artReady}
      onerror={() => {
        failed = src;
        noteMissing(src);
      }}
    />
  {:else}
    {initials}
  {/if}
</span>

<style>
  .face {
    flex: none;
    display: grid;
    place-items: center;
    width: var(--face, 40px);
    height: var(--face, 40px);
    border-radius: var(--r-pill);
    overflow: hidden;
    /* What a photo still loading shows. */
    background: var(--surface-2);
    color: #fff;
    font-size: calc(var(--face, 40px) / 3);
    line-height: 1;
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
</style>
