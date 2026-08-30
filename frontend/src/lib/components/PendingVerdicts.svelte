<script>
  /**
   * §6.0's pending-verdicts banner. Spec v2.1 §6.0 (M2), §7.3; proposals 21 and 150.
   *
   * Proposal 21, verbatim: wide — "You watched {titles} — a quick verdict keeps your profile
   * sharp." (CTA "Rate now"); compact — "Watched, not rated: {titles}" (CTA "Rate"). "At most
   * three titles are named; beyond that the list reads '{title}, {title} and N more'."
   *
   * The sentence and the link are both the SERVER's. Proposal 150: "The CTA enters the §6.1
   * queue with the named titles at its head — a prompt that names titles and then presents a
   * different one is worse than no prompt." So this component never composes copy from
   * `named`, and never navigates to a bare `/rate`: it renders `banner.copy` and follows
   * `banner.cta.route`, and if that route does not carry every named title as a repeated
   * `head` parameter, `bannerHref` returns null and the CTA does not render at all.
   *
   * NOT §7.3's finish prompt (proposal 150). That one is armed by a playback event, names one
   * title, and its first tap writes `seen`; this one is a standing element over titles that are
   * already seen, and it writes nothing.
   */
  import { bannerHref, bannerLabel, bannerText } from '$lib/home.svelte.js';

  let { banner } = $props();

  // Proposal 21 gives two registers, not one sentence at two sizes. Which one to render is a
  // viewport question, asked of the viewport rather than answered with `display: none` — a
  // hidden copy is still in the accessibility tree and still on the clipboard.
  //
  // Bound to the width rather than to a `matchMedia` listener, because it is the idiomatic
  // rune form and reads as data. 720 is the breakpoint the rest of the shell already uses.
  // (Measured caveat for whoever tests this: under Chrome's device-metrics emulation neither
  // `resize` nor a media query's `change` event fires at all, so the register is only correct
  // on load there. Both fire on a real browser resize; drive it with a reload to assert it.)
  let width = $state(1024);
  const compact = $derived(width <= 720);

  const text = $derived(bannerText(banner, { compact }));
  const href = $derived(bannerHref(banner));
  const label = $derived(bannerLabel(banner, { compact }));
</script>

<svelte:window bind:innerWidth={width} />

{#if banner && banner.count > 0 && text}
  <div class="banner" role="status" data-testid="pending-verdicts" data-count={banner.count}>
    <div class="text">
      <div class="line" data-testid="pending-verdicts-copy">{text}</div>
      <div class="data names">
        {banner.count} seen, no verdict · {banner.named.length} named
      </div>
    </div>
    {#if href}
      <a class="btn-primary" {href} data-testid="pending-verdicts-cta" data-head={banner.head_title_ids.join(' ')}>
        {label}
      </a>
    {:else}
      <!-- The link could not be shown to carry the titles the sentence just named. Saying so
           is better than sending someone into a queue that starts somewhere else. -->
      <span class="data broken" data-testid="pending-verdicts-no-cta">
        queue link unavailable — it would not start with the titles named
      </span>
    {/if}
  </div>
{/if}

<style>
  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    padding: 12px 15px;
    margin-bottom: 16px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
  }
  .line {
    font-size: 14px;
  }
  .names {
    margin-top: 3px;
  }
  .broken {
    color: var(--ember-lift);
  }
</style>
