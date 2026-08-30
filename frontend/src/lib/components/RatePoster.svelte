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
   * There are no poster images in the bundle — only paths — so this is PosterCard's stable
   * tinted panel: a hue derived from the title's own name, identical on every surface and
   * across reloads, because a card that changes colour every render reads as a bug.
   */
  import { hueOf } from '$lib/rate.svelte.js';

  /**
   * `showName` exists because §6.8's card grammar puts the title on the poster, and a surface
   * that also prints the title beside the poster would then say it twice. The sweep card has a
   * 26 px heading two centimetres away; the battle card does not.
   */
  let { title, showName = true } = $props();

  const h = $derived(hueOf(title?.name ?? String(title?.id ?? '')));
</script>

<div
  class="poster"
  data-testid="rate-poster"
  data-title-id={title?.id}
  style:background="linear-gradient(150deg, hsl({h} 22% 17%), hsl({(h + 40) % 360} 18% 11%))"
>
  {#if showName}
    <span class="scrim"></span>
    <span class="name">{title?.name ?? '—'}</span>
  {/if}
</div>

<style>
  .poster {
    aspect-ratio: 2 / 3;
    border-radius: 10px;
    border: 1px solid var(--line);
    position: relative;
    overflow: hidden;
    display: block;
    width: 100%;
    transition: border-color 0.18s ease, transform 0.18s ease;
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
