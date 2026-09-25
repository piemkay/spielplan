<script>
  /**
   * The rating surfaces' poster. §6.8: "Poster-forward 2:3 cards."
   *
   * Deliberately inert. §6.1 says of the battle card "nothing tappable inside the poster
   * cards" — the poster *is* the button on that surface, so this component renders and never
   * handles. It also carries no badge: the anchoring rule (§6.1, Cosley 2003, extended to
   * battles by proposal 34) forbids tier, score, σ or rank before the answer, and the §8
   * stage-10 cold badge is still a statement about the model, which is why the server does not
   * even send `placement` on this card.
   *
   * The art is the same-origin poster route's (decision 483), drawn over PosterCard's stable
   * tinted panel: a hue derived from the title's own name, identical on every surface and across
   * reloads, which is what shows while the image loads and what stays when the title has none
   * (a designed state, not an error). The image is dropped on error rather than left broken.
   *
   * It takes the title under either key, `id` or `title_id` (`lib/art.js` says why), so every
   * surface that shows a title as a 2:3 card can hand it the payload it already holds.
   */
  import { hueOf } from '$lib/rate.svelte.js';
  import { posterSrc, titleIdOf } from '$lib/art.js';

  /**
   * `showName` exists because §6.8's card grammar puts the title on the poster, and a surface
   * that also prints the title beside the poster would then say it twice. The sweep card has a
   * 26 px heading two centimetres away; the battle card does not.
   */
  let { title, showName = true } = $props();

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
  <!-- Keyed on the URL: a browser keeps painting an <img>'s old picture until its new `src` has
       loaded, and this component is reused as a battle, a Tonight pair or the Rank queue moves
       on - so for the whole fetch the next title's name sat over the last title's art, on a card
       whose poster is the answer button (§6.1). A fresh element per title shows the tinted panel
       until the right art arrives. -->
  {#key src}
    {#if src && failed !== src}
      <!-- `alt=""`: the title is printed beside or over the poster, and a screen reader reading
           it twice is the card saying it twice. Eager, because a Rate card is on screen the
           moment it is drawn and the next one was preloaded during the reveal hold. -->
      <img
        class="art"
        {src}
        alt=""
        loading="eager"
        decoding="async"
        draggable="false"
        onerror={() => (failed = src)}
      />
    {/if}
  {/key}
  {#if showName}
    <span class="scrim"></span>
    <span class="name">{title?.name ?? '—'}</span>
  {/if}
</div>

<style>
  .poster {
    /* design.css's `.poster` already sets the frame — aspect ratio, radius, border,
       positioning and clipping. Only what is this component's own belongs here. */
    display: block;
    width: 100%;
    transition: border-color 0.18s ease, transform 0.18s ease;
  }
  /* Over the tinted panel and under the scrim and the name. Inert to the finger: §6.1's long
     press on a battle poster is a decisive duel, and iOS's image callout would take the same
     press for itself - so the image neither receives the pointer nor offers the callout. */
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
    background: linear-gradient(to top, rgba(13, 13, 15, 0.88), rgba(13, 13, 15, 0));
  }
  /* §6.8's card grammar: the title sits bottom-left over the scrim. */
  .name {
    position: absolute;
    left: 10px;
    right: 10px;
    bottom: 9px;
    font-size: 13px;
    font-weight: 500;
    line-height: 1.2;
    text-wrap: pretty;
  }
</style>
