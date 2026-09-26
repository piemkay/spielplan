<script>
  /**
   * §6.1's sweep card: "one title card → `Liked / Fine / Disliked` + `Not seen` + `Skip` +
   * persistent Undo".
   *
   * What is deliberately absent is the specification. There is no predicted class here, no
   * ledger score, no σ, no tier badge — §6.1 cites Cosley 2003 and the server enforces it with
   * an allow-list, so this component has nothing to withhold because it was never sent
   * anything to leak. It also caches nothing between cards: a client-side "we think you'll
   * like this" reconstructed from an earlier response would defeat the whole arrangement.
   *
   * The card's payload is proposal 40's: poster, title, `{year} · {runtime}` in the data voice,
   * the queue reason, and one recall aid — "a plot logline, not a DNA evidence quote. The
   * user's task on this card is to remember whether they saw it". Genre is not on this wire
   * shape; the two facts that are, are the two that get rendered.
   *
   * After the tap, and only then, the reveal replaces the verdict strip (proposal 42), phrased
   * by the server ("we'd have guessed the same", with its number only behind Show the model --
   * decision 491) or suppressed with its reason when the person has no fit of their own yet
   * (proposal 153).
   */
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { metaLine } from '$lib/rate.svelte.js';

  let {
    card,
    reveal = null,
    holding = false,
    busy = false,
    pending = null,
    showModel = false,
    onVerdict,
    onNotSeen,
    onSkip,
    onContinue
  } = $props();

  const title = $derived(card?.title ?? {});
  // P(seen) rides under `model`, which the server strips for a viewer with Show the model off
  // (decision 486); read here only while the viewer's switch is on as well.
  const pSeen = $derived(showModel ? (card?.model?.p_seen ?? null) : null);
  const labels = $derived(
    card?.verdict_labels ?? [
      [0, 'disliked'],
      [1, 'fine'],
      [2, 'liked']
    ]
  );
</script>

<article class="sweep" data-testid="rate-sweep-card" data-card-token={card?.token}>
  <div class="poster-slot">
    <RatePoster {title} showName={false} />
  </div>

  <div class="body">
    <h2 data-testid="rate-card-title">{title.name ?? '—'}</h2>
    <div class="data-lg" data-testid="rate-card-meta">{metaLine(title)}</div>

    <!-- §6.8: "every shelf, recommendation, question and conflict carries a one-line why".
         Proposal 39 puts it under the meta line and above the recall aid. -->
    <p class="why" data-testid="rate-queue-reason">{card?.reason ?? ''}</p>
    {#if pSeen != null}
      <!-- §6.8's data voice, the number beside its name (decision 486). -->
      <p class="data model" data-testid="rate-queue-p-seen">P(seen) {pSeen.toFixed(2)}</p>
    {/if}

    <!-- `substituted_for` stays on the wire and is not rendered here any more. The line it drove
         -- "a battle was due in this slot, serving a sweep card instead" -- told a member about the
         block machine's plan rather than about the film in front of them, and on a pinned title
         it answered a search with an apology. The counter above names the card's own type, which
         is the one fact the person needs; decision 486 keeps the rest with the model.
         [§6.1, §6.8; M4.10 finding 21; C4.5 and C5.5 of the 2026-09-25 household test] -->

    {#if title.recall_aid}
      <p class="recall" data-testid="rate-recall-aid">{title.recall_aid}</p>
    {/if}
  </div>

  <div class="strip">
    {#if holding && reveal}
      <!-- Proposal 42: attached to the card just rated, ~1.2 s, clears on any next action. -->
      <button
        class="reveal"
        data-testid="rate-reveal"
        data-reveal-available={reveal.available ? 'true' : 'false'}
        data-reveal-agreed={reveal.agreed ? 'true' : 'false'}
        onclick={onContinue}
      >
        <span class="eyebrow">PREDICTION</span>
        <span class="reveal-text">{reveal.text}</span>
        <span class="data next">next card →</span>
      </button>
    {:else}
      <!-- Proposal 52: lowercase, worst → best, matching the stored ordinal. -->
      <div class="verdicts" role="group" aria-label="Your verdict">
        <!-- The tapped answer stays lit and says it is being saved while the others wait: A4 of
             the 2026-09-26 household test was every button greying out at once, with nothing
             saying which answer had been taken. -->
        {#each labels as [value, label] (value)}
          <button
            class="verdict v{value}"
            class:picked={pending === `verdict-${value}`}
            data-testid="rate-verdict-{value}"
            data-verdict-label={label}
            aria-busy={pending === `verdict-${value}`}
            disabled={busy}
            onclick={() => onVerdict(value)}
          >{label}</button>
        {/each}
      </div>
      <div class="secondary">
        <!-- Owner decision 2026-08-29: one seen-state control. A title you cannot remember
             is plain `unseen` (§4.2) — there is no third state to offer. -->
        <button
          class="text"
          class:picked={pending === 'not_seen'}
          data-testid="rate-not-seen"
          aria-busy={pending === 'not_seen'}
          disabled={busy}
          onclick={onNotSeen}
        >
          not seen
        </button>
        <!-- Proposal 38: writes no observation, suppresses the redraw for this sitting,
             and is covered by the persistent Undo like everything else. -->
        <button
          class="text"
          class:picked={pending === 'skip'}
          data-testid="rate-skip"
          aria-busy={pending === 'skip'}
          disabled={busy}
          onclick={onSkip}
        >
          skip
        </button>
      </div>
    {/if}
  </div>
</article>

<style>
  .sweep {
    display: grid;
    grid-template-columns: 210px minmax(0, 1fr);
    grid-template-rows: 1fr auto;
    gap: 14px 20px;
    padding: 16px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    animation: fadeIn 0.15s ease;
  }
  .body {
    display: flex;
    flex-direction: column;
    min-width: 0;
    grid-column: 2;
    grid-row: 1;
  }
  .poster-slot {
    grid-column: 1;
    grid-row: 1 / span 2;
  }
  h2 {
    margin: 0;
    font-size: 26px;
    font-weight: 700;
    line-height: 1.12;
    text-wrap: pretty;
  }
  [data-testid='rate-card-meta'] {
    margin-top: 6px;
  }
  .why {
    margin: 10px 0 0;
  }
  .recall {
    margin: 12px 0 0;
    font-size: 13.5px;
    line-height: 1.5;
    color: var(--ink-3);
    max-width: 56ch;
    text-wrap: pretty;
  }
  .strip {
    grid-column: 2;
    grid-row: 2;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .verdicts {
    display: flex;
    gap: 8px;
  }
  .verdict {
    flex: 1;
    /* `min-width: auto` is a flex item's default, so the longest label ("disliked") would
       refuse to shrink and the other two would pay for it — three buttons of three widths. */
    min-width: 0;
    min-height: var(--touch);
    padding: 16px 10px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line-2);
    background: var(--card-raised);
    color: var(--ink-2);
    font-family: var(--mono);
    font-size: 15px;
    cursor: pointer;
    transition: border-color 0.12s ease, background 0.12s ease, color 0.12s ease;
  }
  .verdict:hover:not(:disabled),
  .verdict:focus-visible {
    border-color: var(--ember);
    color: var(--ink);
  }
  .verdict:active:not(:disabled) {
    background: var(--ember-wash);
  }
  .verdict:disabled {
    opacity: 0.45;
    cursor: default;
  }
  /* The answer in flight: selected, in §6.8's one accent, while its neighbours wait dimmed. */
  .verdict.picked:disabled {
    opacity: 1;
    border-color: var(--ember);
    background: var(--ember-wash);
    color: var(--ink);
  }
  .text.picked:disabled {
    color: var(--ink-2);
  }
  .model {
    margin: 4px 0 0;
  }
  .secondary {
    display: flex;
    gap: 18px;
  }
  .text {
    background: none;
    border: none;
    padding: 6px 0;
    color: var(--ink-4);
    font-family: var(--mono);
    font-size: 11.5px;
    text-decoration: underline dotted;
    text-underline-offset: 4px;
    cursor: pointer;
  }
  .text:hover:not(:disabled) {
    color: var(--ink-2);
  }
  .reveal {
    display: flex;
    flex-direction: column;
    gap: 5px;
    width: 100%;
    min-height: 76px;
    padding: 14px 15px;
    text-align: left;
    border-radius: var(--r-sm);
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    cursor: pointer;
    animation: fadeIn 0.2s ease;
  }
  .eyebrow {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    color: var(--ink-4);
  }
  .reveal-text {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink);
  }
  .next {
    color: var(--ink-5);
  }

  /* Proposal 126: on action-dense surfaces the compact layout puts a full-bleed action bar in
     the thumb arc, with the secondary actions as text beneath it. That only works if the
     answer is *reachable* — a full-width poster pushes the verdict strip below the fold on a
     812 px phone, so the poster narrows and moves beside the text rather than above it. The
     card stays poster-forward (§6.8); it stops being poster-only. */
  @media (max-width: 720px) {
    .sweep {
      grid-template-columns: 108px minmax(0, 1fr);
      gap: 12px;
      padding: 12px;
    }
    .poster-slot {
      grid-row: 1;
    }
    h2 {
      font-size: 19px;
    }
    .recall {
      font-size: 12.5px;
      margin-top: 8px;
    }
    .strip {
      grid-column: 1 / -1;
      grid-row: 2;
    }
    .verdicts {
      gap: 6px;
    }
    .verdict {
      padding: 18px 6px;
      border-radius: 2px;
      font-size: 14px;
    }
  }

  /* And the floor on the axis design.css never sets. §6 preamble's "48 px targets" carries no
     axis in it, and this was the control furthest under it in the tree: `skip` is four characters
     of an 11.5 px mono face with `padding: 6px 0`, so the coarse block's
     `button { min-height: var(--touch) }` made it 48 tall and left it 25 wide. Proposal 38 makes
     that button the escape hatch from a question that cannot be answered, on §4's most repeated
     interaction, and it sits 18 px from `not seen` — so a thumb that misses it lands on the
     control that WRITES a §4.2 observation.

     Width only, and in this component rather than in design.css: that block sets no width at all
     on purpose, because a blanket `min-width` on its six primitives would reach every narrow
     control in the app at once. The dotted underline is unaffected — a button's text decoration
     is drawn over its inline content, not over its box.
     [§6 preamble; proposal 38; review cycle 3: M415-C3-CSS-01] */
  @media (pointer: coarse) {
    .text {
      min-width: var(--touch);
    }
  }
</style>
