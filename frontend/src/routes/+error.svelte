<script>
  /**
   * The app's own page for a load error. Spec v2.1 §6.8 (one design language), §3.1 ("render an
   * explicit state instead of erroring").
   *
   * WHAT REACHES IT, named rather than assumed. There is no `load` function anywhere in
   * `src/routes` — `+layout.js` exports `ssr` and `prerender` and nothing else — so "a throw
   * from a load" is not one of them, and a comment that said so pointed the next reader at a
   * path with no instances. The two that are real:
   *
   * 1. a client-router 404. `app.py`'s SPA fallback answers any unknown GET with `index.html`,
   *    the router matches no route, and because `ssr = false` makes the first navigation an
   *    unhydrated one, SvelteKit's `server_fallback` renders this page rather than reloading the
   *    address it is already on;
   * 2. a route chunk that fails to import while `_app/version.json` has NOT moved — the
   *    appliance went away mid-session, which is §6 preamble's own case. Where the version HAS
   *    moved, `+layout.svelte`'s `beforeNavigate` has already taken the deploy (finding 23), so
   *    this page is what is left when a reload is not the answer.
   *
   * Either way the alternative was SvelteKit's built-in page: a bare light-themed "Internal
   * Error" on white, outside the design system, with no way back into the dark shell — on a
   * phone in standalone mode, where there is no address bar to type into, it is a dead end. This
   * is the same failure the app answers everywhere else with a stated sentence and a door.
   *
   * `design.css` is imported here as well as in the layout as belt and braces, not because the
   * layout can be absent: SvelteKit keeps the root layout in the branch when it swaps in a root
   * error page, so `+layout.svelte`'s own import is always in force. A page whose whole job is
   * to render when something upstream did not is the wrong place to depend on an upstream
   * import, and the cost is nothing — the import is deduplicated at build time.
   * [M4.15 finding 19, fe-15; review cycle 2: M415-C2-COMP-04]
   */
  import '$lib/design.css';
  import { page } from '$app/stores';
</script>

<div class="page">
  <div class="card fail" data-testid="app-error">
    <span class="data">ERROR {$page.status}</span>
    <h1>That screen did not load</h1>
    <!-- The framework's message, in the register §6.8 gives every other reason in the app. It is
         not always a sentence a household can act on, which is why the two doors below do not
         depend on reading it. -->
    <p class="why">{$page.error?.message || 'No reason was given.'}</p>
    <div class="doors">
      <button class="btn-primary" onclick={() => location.reload()}>Reload</button>
      <a class="btn-ghost" href="/">Home</a>
    </div>
    <!-- The two doors repair the two different failures named in the script above, and the card
         said which for neither. On the one of them any test in this repository drives - a
         client-router 404 - Reload is a no-op by construction: `app.py`'s SPA fallback answers
         the same unknown address with index.html and 200, the router matches no route again, and
         this card renders again. On a phone in standalone mode, where there is no address bar,
         that is a door a person can press for ever. §6.8 asks for a sentence somebody can act on,
         and "which of these two" is the only thing left to say once the framework's message has
         been rendered above. [§6.8; review cycle 3: M415-C3-COMP-02] -->
    <p class="why">
      Reload if you were in the middle of using the app; Home if you followed a link or typed the
      address.
    </p>
  </div>
</div>

<style>
  .page {
    display: grid;
    place-items: center;
    padding: 24px;
  }
  .fail {
    width: min(420px, 100%);
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
  }
  .doors {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  /* The ghost button is an `<a>` here, and both halves of the class DO reach it: `.btn-ghost`
     carries the padding box, and it is named in design.css's coarse-pointer list, which a bare
     `a` would not be. Both hold, because `.doors` is a flex container and a flex item is
     blockified — a `min-height` on an inline box would have been inert, which is the trap this
     comment used to describe and is not the one on this element.

     What is left for these three lines is what the class cannot do from design.css: centre the
     label inside the 48 px box the floor makes (without it the text sits at the top of it), and
     take `a { color: var(--ember) }` off, because §6.8 spends the accent on selection and
     primary actions and a door out of an error page is neither. The repair for an anchor the
     coarse list really does miss is never a wider selector there — it is a scoped block in the
     component, which `NavRail`, `AccountChip` and the header badge each carry.
     [§6.8; review cycle 2: M415-C2-CSS-08] */
  .doors a {
    display: inline-flex;
    align-items: center;
    color: var(--ink-3);
  }
</style>
