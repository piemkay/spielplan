<script>
  /**
   * The app shell. Spec v2.1 §6 preamble (phone-first PWA), §6.8 (design language),
   * §3.2 (the account chip switches profiles, gated by the per-user PIN).
   *
   * Surface names are normative (§6): Home / Rate / Tonight / Rank / Map / Taste (+ Admin).
   * M0 ships Home; the rest are present as destinations that say what milestone owns them,
   * because a nav that hides half the app teaches the wrong shape.
   *
   * §6.7's model rail is mounted HERE, once, for the same reason the account chip is: the
   * per-user toggle that governs it is in that dropdown, which is on every screen. Mounted on
   * Home alone it left the three surfaces that write the most model events substituting partial
   * logs — Rate's per-response echo, Rank's single-element log, Tonight's embedded five
   * [M4.9 finding 25].
   *
   * M4.15 gives this file the other half of its job: the box, not just what is in it. §6's
   * preamble — "responsive PWA, phone-first, installable, service-worker shell cache" — is
   * normative and had no owner, so the shell's height, its top inset, what a failed boot means
   * and what a lost session does were each decided nowhere. They are decided here, once,
   * because a rule that lives in a surface is not a rule.
   */
  import '$lib/design.css';
  import { onMount } from 'svelte';
  import { page, updated } from '$app/stores';
  import { beforeNavigate, goto } from '$app/navigation';
  import { bootstrap, session, clearUser, landingRoute, refreshUser } from '$lib/session.svelte.js';
  import { onUnauthenticated, post } from '$lib/api.js';
  import { reset as resetRank } from '$lib/rank.svelte.js';
  import { closeRail, followShowModel, modelRail, toggleRail } from '$lib/rail.svelte.js';
  import AccountChip from '$lib/components/AccountChip.svelte';
  import ModelRail from '$lib/components/ModelRail.svelte';
  import NavRail from '$lib/components/NavRail.svelte';

  let { children } = $props();

  const canAdmin = $derived(
    (session.user?.nav?.account ?? []).some((entry) => entry.key === 'admin')
  );

  // §3.2: "admin routes re-prompt after 24 h". The gate is server-side and returns a 401 that
  // an admin can do nothing about from an admin page, so the shell says what it is and where
  // to go. Only on the surfaces it actually blocks — elsewhere it is not yet true of anything.
  const needsReauth = $derived(
    !!session.user?.admin_reauth_required &&
      ($page.url.pathname.startsWith('/admin') || $page.url.pathname.startsWith('/setup'))
  );

  // sec-05: a PIN session is refused by `POST /api/auth/reauth` however good the password
  // typed into it is (§3.2 makes it a switch convenience, not a sign-in), so the banner offers
  // no form there — and it must not claim the session is "more than 24 hours old", which is
  // not why this one is blocked.
  const pinSession = $derived(session.user?.auth_method === 'pin');
  let reauthPassword = $state('');
  let reauthError = $state('');
  let reauthBusy = $state(false);

  const bare = $derived(
    $page.url.pathname.startsWith('/setup') ||
      $page.url.pathname.startsWith('/login') ||
      $page.url.pathname.startsWith('/account/password')
  );

  // §6.7's toggle, read from the preference rather than from a payload. Home derived this
  // from `hasModelAnnotations(home)` — decision 117's gate asked of the `/api/home` response —
  // which is a Home fact the shell does not have and the other three surfaces never had. Rank
  // and Rate already read the preference directly (`rank/+page.svelte:44`,
  // `rate/+page.svelte:68`); this is that same line once, on the chrome that carries the drawer,
  // and `session.svelte.js` keeps it current on every route.
  const showModel = $derived(!!session.user?.show_model);

  // Turning the preference off takes an open drawer with it — `+page.svelte` did this before
  // the mount moved and it has to survive the move. The rule is in `rail.svelte.js`, where it
  // is falsifiable without a browser; this is only the wiring.
  $effect(() => {
    followShowModel(session.user?.show_model);
  });

  /** Proposal 118: the rail "must be reachable in two taps" and from a keyboard shortcut. The
   *  shortcut moved with the drawer — a rail on every surface reached by `m` on one of them is
   *  finding 25 one layer down. `bare` routes have no shell to mount it in, so it is not a
   *  surface the drawer is reachable from. */
  function onRailKey(event) {
    if (event.key !== 'm' || event.metaKey || event.ctrlKey || event.altKey) return;
    const el = event.target;
    if (el instanceof HTMLElement) {
      const tag = el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable) return;
    }
    toggleRail(showModel && !bare);
  }

  // Routes reachable before there is a signed-in user.
  const PUBLIC = ['/login', '/setup', '/account/password'];

  /**
   * Where this person belongs is not known — which is not the same as their not being signed in.
   *
   * `bootstrap()` swallows a failed `/setup/state` into null, and `landingRoute()` reads
   * `setup.required` to tell §3.1's first admin from a signed-out member. With that read missing
   * and `/auth/me` having ANSWERED 401, /setup and /login are both live answers and this device
   * holds no bit that separates them: sec-14 cut the anonymous payload, and here there is no
   * payload at all. `guard()` is right to refuse to route on a guess — but refusing to route is
   * not a state, and what stood in its place was the authed shell: the header, an account chip
   * reading "signed out", no nav links, and the surface underneath printing `api/deps.py`'s
   * sentence, with Log out the only way forward. That is the same category decision 271 fixed
   * for `hasBundle` — a rendering of a fact nobody read — and §3.1 asks for an explicit state
   * instead. So the shell says which read is missing and asks again on decision 283's timer,
   * which lands both people where they belong the moment `/setup/state` answers.
   *
   * `bare` still wins over this, and deliberately: /login's form and /setup's wizard each
   * re-read for themselves, so a person who is already on one of them is not taken off it.
   * [decision 286; §3.1, §6.8]
   */
  const landingUnknown = $derived(
    session.booted && !session.offline && !session.setup && !session.user
  );

  let retrying = $state(false);

  onMount(() => {
    // The seam `api.js` opened (finding 14). Before it nothing above the fetch reacted to a 401:
    // each store turned its own into its own red line, so after a rotated `SESSION_SECRET`, a
    // restored backup or the hourly prune the person met `api/deps.py`'s "not signed in" on
    // Rate, then on Rank, then on Home — with their name still in the chip and Log out the only
    // way forward. `goto` lives on this side of the seam so that module keeps no router and no
    // DOM; which 401s are a lost session is api.js's decision, and §3.2's admin re-prompt and
    // the credential routes never reach here.
    onUnauthenticated((reason) => {
      if (reason === 'password-change') {
        // §3.1's forced change seen from a tab that was already open when an admin reset the
        // password: the server refuses every write, and the shell's job is to say where the one
        // screen that clears it is rather than repeat the refusal on each surface in turn.
        goto('/account/password');
        return;
      }
      clearUser();
      resetRank();
      goto('/login');
    });

    // The event the browser fires that means "ask again" — the fast path, and not the only one:
    // it reports the INTERFACE, and what this shell is waiting for is the appliance. See the
    // timer below for the half it does not cover.
    window.addEventListener('online', reconnect);

    // Through `reconnect()`, which is this body plus the single-flight flag. Inlined, the initial
    // boot was a FOURTH way in that the guard below could not see: the `online` listener is
    // registered before it starts, so an interface that flaps while the first three reads are in
    // flight — up to `api.js`'s deadline — opened a second concurrent `bootstrap()` writing into
    // the same `session` object. The second answer painted a working shell and the first one's
    // stale `/auth/me` then rejected into `session.offline = true`, putting the unreachable card
    // over a shell on a network that was back. Decision 283's tick clears it five seconds later,
    // which is exactly why nothing would ever have been pointed at it.
    //
    // `retrying` is false at mount, so the boot itself is unchanged: same reads, same `guard()`,
    // same `catch` that keeps a deep link honest when a read added to `bootstrap()` later throws
    // (the missing `try` here is what turned finding 15 from a failed read into a sign-out).
    // [decision 283; review cycle 2: M415-C2-SHELL-02]
    reconnect();

    return () => {
      onUnauthenticated(null);
      window.removeEventListener('online', reconnect);
    };
  });

  /** Ask again: the retry button, the browser's `online` event, and the timer below. The guard
   *  is what keeps the three from overlapping — two boots in flight would race each other's
   *  writes into one `session` object, and the second answer would win whatever it said. */
  async function reconnect() {
    if (retrying) return;
    retrying = true;
    try {
      await bootstrap();
    } catch {
      session.offline = true;
    } finally {
      retrying = false;
    }
    await guard($page.url.pathname);
  }

  /**
   * The other way back, and the one that does not depend on being told (decision 283).
   *
   * `online` fires for the INTERFACE. What this shell is waiting for is the appliance, and §2
   * puts it on a LAN or a Tailscale address: a box that is restarting, a route that has not
   * re-established and a captive portal all come back with the interface up the whole time, so
   * no event is owed and none arrives. The card would then stand until someone reloaded by hand
   * — which in an installed standalone web view means finding a reload gesture iOS does not
   * offer, and §6's preamble makes that install the primary shape.
   *
   * It costs nothing when the app is working, because it does not exist then: the effect is
   * armed by `session.offline` and its cleanup disarms it the instant a read lands. That is also
   * why this is a timer and not `tonight.svelte.js`'s socket — there is no connection to keep,
   * and asking is the only way to find out.
   *
   * `landingUnknown` arms it for the same reason and on the same terms: a `/setup/state` that did
   * not arrive is a read this shell cannot route without, and no event is owed for it either.
   * Both states end the instant a read lands, and the cleanup disarms the timer with them.
   * [decision 286]
   */
  const RETRY_MS = 5_000;
  $effect(() => {
    if (!session.offline && !landingUnknown) return;
    const timer = setInterval(reconnect, RETRY_MS);
    return () => clearInterval(timer);
  });

  // Every navigation is guarded, not only the landing one: a deep link to /rank with no
  // session used to render the authed shell and then fail request by request.
  $effect(() => {
    if (!session.booted) return;
    guard($page.url.pathname);
  });

  async function guard(pathname) {
    // Where a person belongs is decided by two reads, and both of them can fail. While the
    // appliance is unreachable the honest answer is "not known", and routing on a guess is
    // precisely what turned a restarting box into a sign-out — the card below says so instead
    // (§3.1: an explicit state rather than an error).
    if (session.offline) return;
    // The same rule one step further in. `bootstrap()` swallows a failed `/setup/state` into
    // null, so "no wizard owed and nobody signed in" is indistinguishable from "neither read
    // arrived", and `landingRoute()` answers /login for both — which on a first boot whose
    // `/setup/state` failed sends the household's first admin to a sign-in form for an account
    // that does not exist yet, past the wizard that would have created it (§3.1).
    //
    // Refusing to route is only half an answer, and `landingUnknown` above is the other half:
    // `session.user === null` here is the appliance ANSWERING, so this branch is reached with a
    // fact in hand and only the DESTINATION unknown. Left at that, a lapsed member's deep link
    // rendered the authed shell under a chip reading "signed out" and nothing ever moved. The
    // shell now states the missing read and asks again; this return is what keeps it from
    // routing that person on a guess in the meantime. [decision 286]
    if (!session.setup && !session.user) return;
    const target = landingRoute();
    // /setup is bounced for anyone who is not a signed-in admin, not for anyone once the
    // admin step is done (cs-33). §3.1's own sequence continues past that step — connectors,
    // then the bundle — and the old test bounced the operator to Home the instant the admin
    // row existed, which made steps 2 and 3 of a three-step wizard unreachable in the app
    // that ships. An admin revisiting the page is exactly who §3.1 means by "a revisitable
    // page after that" (`api/setup.py`'s own comment); a member is not, and neither is a
    // signed-out stranger — the rule ran only for a signed-IN caller, and sec-14 stopped
    // telling an anonymous one whether an admin exists, so the shell is the only thing left
    // that knows the wizard is over (fe-46). `required` is that bit, for everyone.
    if (pathname === '/setup' && !session.setup?.required && session.user?.role !== 'admin') {
      await goto(target);
      return;
    }
    if (target === '/') {
      if (pathname === '/login') await goto('/');
      return;
    }
    if (!PUBLIC.includes(pathname)) await goto(target);
  }

  // Decision 272's amendment is a claim about a NAVIGATION, so the hook that rewrites navigations
  // has to know about it (review cycle 2: M415-C2-SHELL-03). See `beforeNavigate` below.
  let leaving = false;

  async function logout() {
    // Only the POST is guarded. §3.2 makes logout "clears the session cookie only", and on a
    // LAN/Tailscale app a failed request is the ordinary failure: unguarded, it rejected out
    // of the click handler and left the menu open showing the name of a person whose session
    // the server may already have destroyed (feroutes-logout). The local clears below are the
    // half that must happen either way.
    let ended = true;
    try {
      await post('/auth/logout');
    } catch {
      // Nothing to report and nothing to retry: the cookie is HttpOnly, so the only thing this
      // page can still do about the session is stop acting as though it holds one.
      ended = false;
    }
    clearUser();
    // Sign-out is a client-side navigation, so module-level surface state survives it. Rank's
    // lift is a *pending write naming a bare title id*, and carried into the next person's
    // session it would post a `tier_edit` into their append-only Ledger on the first tap.
    resetRank();
    // And then everything else, by leaving the document — the house pattern `AccountChip`
    // already uses for the PIN switch (decision 272). The three surface stores are module-level
    // `$state` singletons that outlive a client-side navigation, and only Rank was ever cleared:
    // Rate keeps `card`, `session`, `ledger`, `log` and `booted = true`, so the next person's
    // `load()` paints the previous one's card and numbers for a whole round trip; Rank's own
    // `reset()` leaves `tiers`, `tierSet` and `ratedTotal`; and `tonight.svelte.js`'s
    // `bootstrap()` writes state only `if (mine)`, so a member seated in no live room keeps the
    // previous person's step, lobby, ballot slate, ticked approvals and result indefinitely. On
    // the family tablet that is one member's taste shown to another, which is the opposite of
    // what §3.2's per-profile chip and the per-user Ledger exist for. A document load is total
    // by construction — it cannot miss a field a store gains next month, which three hand-written
    // `clearAll()` functions in three files would.
    //
    // ONE navigation, and it NAMES where it goes. `location.reload()` has no destination of its
    // own: it takes whatever is in the address bar, and `await goto('/login')` is no proof that is
    // /login. `clearUser()` above is read by this file's own `guard()` effect, which starts a
    // second `goto('/login')` in the same tick; SvelteKit writes the history entry only after the
    // route's chunk has been imported (`client.js` puts `history.pushState` past
    // `await load_route`), and a superseded navigation returns before that — `goto` resolves
    // either way. The third browser gate has the trace: the logout POST, the /login chunk still in
    // flight, and 1.3 ms later a document navigation to /rate itself, which mounted the Rate
    // surface with nobody signed in. `onMount(load)` is unconditional, so `GET /api/rate`
    // answered 401 long before the next person typed a password; decision 282 is right not to
    // read that as a lost session, so it fell through to the surface, which latched `booted`
    // and `api/deps.py`'s "not signed in" — a sign-in is a client-side navigation, so both
    // of those crossed into the next person's session. `location.assign` cannot lose that race
    // because it is not in it, and `beforeNavigate` below already leaves this document the same
    // way for a new version.
    // [decision 285; §3.2]
    //
    // And only once the server has SAID the session is over (decision 272, amended). The cookie
    // is HttpOnly: a POST that never landed leaves it standing, so leaving re-boots into the
    // session it was meant to end — `/auth/me` answers 200, `landingRoute()` returns `/`, and
    // `guard()` carries the person who just tapped Log out back to Home under their own name.
    // That is the sign-out visibly not happening, which is a worse failure than the module state
    // this exists to clear; totality is only worth anything once there is nothing left to
    // come back to. What survives on that path is the local half and it is the whole of what
    // §3.2 leaves this device able to do: the user forgotten, Rank's pending lift dropped, the
    // sign-in form on screen. Ending a session the server never heard about is not in a browser's
    // gift. [decision 272; §3.2]
    if (ended) location.assign('/login');
    else {
      // And on this branch the deploy hook must keep its hands off. `beforeNavigate` below turns
      // any client-side navigation into `location.href = ...` while `$updated` is up, which for
      // the sixty seconds after a deploy lands is every navigation — including this one. That
      // converts the one `goto` decision 272's amendment chose deliberately back into the
      // document load it exists to avoid: the cookie is HttpOnly and the POST never landed, so
      // the reboot answers `/auth/me` 200 and `guard()` carries the person who just tapped Log
      // out back to Home under their own name. A deploy is taken at a route change the person
      // CHOSE, and a sign-out that could not reach the server is not one.
      leaving = true;
      try {
        await goto('/login');
      } finally {
        leaving = false;
      }
    }
  }

  async function reauth(event) {
    event.preventDefault();
    reauthError = '';
    reauthBusy = true;
    try {
      await post('/auth/reauth', { password: reauthPassword });
      reauthPassword = '';
      // The route answers with the same payload as /auth/me minus the passkey and Jellyfin
      // fields the shell also renders, so re-read rather than assigning the response.
      await refreshUser();
    } catch (err) {
      reauthError = err.message;
    } finally {
      reauthBusy = false;
    }
  }

  /**
   * Take a deploy at a moment the person chose (finding 23).
   *
   * `service-worker.js` calls `skipWaiting()` and deletes every other cache on activate, which
   * is right for a single-host appliance — but it means a tab open across a deploy has lost the
   * chunks it is about to import. SvelteKit's own fallback for that is a native navigation, and
   * it is the correct one; the defect was only *when* it fired. With `version.pollInterval`
   * unset the check happened on the import failure itself, so the reload landed mid-round or
   * mid-block, on the tap that was meant to record a verdict. Polled (`svelte.config.js`) the
   * flag is already up by the time a route changes, and a route change is the one moment the
   * person has just said they are leaving the screen. `willUnload` is excluded because the
   * browser is already doing the navigation and forcing a second one would cancel it, and
   * `leaving` because a sign-out whose POST never landed is the one navigation this shell makes
   * that must NOT become a document load — decision 272's amendment, which `logout()` argues.
   */
  beforeNavigate((nav) => {
    if ($updated && !nav.willUnload && !leaving && nav.to?.url) location.href = nav.to.url.href;
  });
</script>

<svelte:window onkeydown={onRailKey} />

<!-- sec-05, and decision 164's consequence: /setup is `bare` (no shell, no nav) AND it is
     now a revisitable admin surface, so the banner has to be renderable outside the shell
     as well as inside it. A snippet rather than two copies: an admin past 24 h who lands on
     the wizard would otherwise get no banner, no form and a 401 from every step. -->
{#snippet reauthBanner()}
    <!-- sec-05: the affordance here used to be a link to /login, which `guard()` bounces
         straight back for a live session — the banner's only way out led in a circle.
         `POST /auth/reauth` clears the stamp on the session in hand and mints nothing. -->
    <div class="reauth" role="alert" data-testid="admin-reauth">
      {#if pinSession}
        <span>
          This profile was switched into with a PIN, which §3.2 makes a convenience on a
          device someone is already signed in on — not proof of who is holding it. Sign
          in with your password or a passkey to reach the admin view.
        </span>
      {:else}
        <span>
          This admin session is more than 24 hours old (§3.2). Confirm your password to
          carry on.
        </span>
        <form class="reauthform" onsubmit={reauth}>
          <input
            type="password"
            autocomplete="current-password"
            placeholder="password"
            bind:value={reauthPassword}
            aria-label="Password"
          />
          <button class="btn-primary" type="submit" disabled={reauthBusy || !reauthPassword}>
            {reauthBusy ? 'Checking…' : 'Confirm'}
          </button>
        </form>
      {/if}
      {#if reauthError}<span class="reautherr">{reauthError}</span>{/if}
    </div>
{/snippet}

{#if session.loading && !session.booted}
  <div class="boot"><span class="data">connecting…</span></div>
{:else if bare}
  {#if needsReauth}{@render reauthBanner()}{/if}
  {@render children()}
{:else if session.offline}
  <!-- §3.1's "render an explicit state instead of erroring", applied to the state the shell
       cache exists for: the phone has the app and cannot reach the appliance. It replaces the
       shell rather than sitting inside it because every surface behind it would be painting
       from a payload nobody could fetch. The `bare` branch above wins on purpose — a person
       with no session belongs on /login, where the form's own failure now says the same thing
       in the same words (api.js's "the network did not answer"). -->
  <div class="boot">
    <div class="card unreachable" data-testid="appliance-unreachable">
      <h1>Spielplan is not answering</h1>
      <!-- The second sentence is conditional because the first one says why it has to be. The
           read that decides who is holding the phone is the read that failed, and on a COLD boot
           — the one journey the shell cache exists for, and the one `manifest.webmanifest`'s
           `start_url: "/"` lands the installed icon in — `session.user` is null from birth,
           because module `$state` does not survive a document load. Told unconditionally that
           they were still signed in, a member who had tapped Log out and reopened the icon was
           being told the opposite of what the tap did, on a screen with no address bar to reach
           /login from. Same category as decision 271's header pill: the one line of a card that
           states a fact rather than rendering one. [decision 271; §6.8; review cycle 2] -->
      <p class="why">
        The app is cached on this device, so it opened; the appliance did not answer the request
        that says who is holding the phone.
        {#if session.user}
          You are still signed in and nothing was sent.
        {:else}
          Nothing was sent, and nothing on this device has signed anybody out.
        {/if}
      </p>
      <button class="btn-primary" onclick={reconnect} disabled={retrying}>
        {retrying ? 'Trying' : 'Try again'}
      </button>
    </div>
  </div>
{:else if landingUnknown}
  <!-- §3.1's "render an explicit state instead of erroring", applied to the half-answer: the
       appliance said nobody is signed in and did not say whether an admin exists, and /setup and
       /login are both live readings of that. The authed shell is what stood here — chrome for a
       person the appliance has just said is not signed in — which is a rendering of a fact
       nobody read, the thing decision 271 took out of the header. Same card, different sentence,
       because the difference is what a person can do about it. [decision 286] -->
  <div class="boot">
    <div class="card unreachable" data-testid="landing-unknown">
      <h1>Spielplan did not finish answering</h1>
      <p class="why">
        This device was told that nobody is signed in here. It also asked whether this household
        has been set up yet, and that answer did not arrive — so it cannot tell the sign-in page
        from the first-boot wizard. Nothing was sent, and it is asking again.
      </p>
      <button class="btn-primary" onclick={reconnect} disabled={retrying}>
        {retrying ? 'Trying' : 'Try again'}
      </button>
    </div>
  </div>
{:else}
  <div class="shell">
    <header>
      <span class="brand">SPIELPLAN</span>
      <div class="spacer"></div>
      {#if session.hasBundle === false}
        <!-- `=== false`, not `!`, because null is now "/config did not answer" and this badge is
             the one line of the shell that states a fact about the household rather than
             rendering one: it asserted "no bundle imported" from the initial value whenever that
             read failed, to households that had imported one (decision 271).
             §3.1: a bundle-less app is a legal state, said out loud rather than crashed on.
             Only an admin gets a link out of it: importing is §6.6's Data tab, and offering a
             member a door they will meet a 403 behind is worse than stating the fact. -->
        {#if canAdmin}
          <a class="nobundle data" href="/admin/data">no bundle imported</a>
        {:else}
          <span class="nobundle data">no bundle imported</span>
        {/if}
      {/if}
      {#if showModel}
        <!-- §6.7 calls the rail "the primary M2 debugging instrument" and proposal 118 wants it
             two taps away. In the header it is two taps from every authed surface, which is
             what finding 25 asked for; on Home it was two taps from one. -->
        <button
          class="btn-ghost railbtn"
          onclick={() => toggleRail(showModel)}
          data-testid="model-rail-open"
        >
          Model log
          <span class="data railkey">m</span>
        </button>
      {/if}
      <AccountChip onLogout={logout} />
    </header>
    <div class="body">
      <NavRail />
      <main>
        {#if needsReauth}{@render reauthBanner()}{/if}
        {@render children()}
      </main>
    </div>
  </div>
  <!-- The one drawer. Outside `.shell` because it is fixed-position chrome rather than a row in
       the layout, and inside this branch because `bare` routes have no chip to open it from.
       `suppressed` is Home's, published into `rail.svelte.js` by `/` — the shell has no Home
       payload of its own and must not acquire one. -->
  <ModelRail open={modelRail.open} onClose={closeRail} suppressed={modelRail.suppressed} />
{/if}

<style>
  /* The pair, twice, and in this order (§6 preamble; M4.15 finding 2).
     In an iOS Safari tab `100vh` is the LARGE viewport — 745 px on an iPhone 13 — while 664 px
     are visible with the toolbar expanded, so a shell sized to it puts the 61 px bottom bar
     inside the 81 px strip the toolbar covers. And because the document is then exactly the
     layout viewport, nothing overflows, so Safari never has a reason to collapse the toolbar
     and reveal it: the surface switcher is simply not there. Every member's first session
     happens in a tab, because §3.1 puts the install after the sign-in. `100dvh` is the visible
     viewport; the `100vh` above it is the fallback iOS < 15.4 keeps, which is why the same
     property is declared twice rather than wrapped in an @supports. Not `maximum-scale=1`, which
     takes pinch zoom from everyone, and not `-webkit-fill-available` on body, which breaks the
     bare pages' `min-height: 100vh` centring. */
  .boot {
    height: 100vh;
    height: 100dvh;
    display: grid;
    place-items: center;
    background: var(--ground);
  }
  .shell {
    display: flex;
    flex-direction: column;
    height: 100vh;
    height: 100dvh;
  }
  .unreachable {
    width: min(420px, 100%);
    margin: 24px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  .unreachable h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
  }
  /* The top inset, in the base rule and again in the phone override below (decision 279).
     `app.html:5` sets `viewport-fit=cover` and `:12` sets
     `apple-mobile-web-app-status-bar-style=black-translucent`, so the installed app's web view
     starts at the physical top edge with the status bar drawn over it — 47 px on an iPhone 13,
     59 px from the 14 Pro on. Without the inset the whole 54 px header is under it, and the
     account chip, a 32 px control centred in that row, is not merely hard to hit: a tap where it
     appears to be is a tap on the status bar, which scrolls to top. `env()` resolves to 0 on
     every other engine UNLESS one is asked to report an inset, so nothing moves on desktop or
     in a Safari tab — which is why the DEVICE fact stays owed in `docs/TESTING.md` (decision
     281), while `06-responsive.spec.js` sends Chromium an inset and measures the two rules
     composed, and a static guard holds the rule on the engines with no CDP. */
  header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 20px;
    padding-top: env(safe-area-inset-top);
    height: calc(54px + env(safe-area-inset-top));
    flex: none;
    border-bottom: 1px solid var(--line);
    background: var(--ground-raised);
    position: relative;
    z-index: 60;
  }
  .brand {
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 0.13em;
  }
  .spacer {
    flex: 1;
  }
  .reauth {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px 14px;
    flex-wrap: wrap;
    margin-bottom: 16px;
    padding: 12px 15px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
    font-size: 13px;
  }
  .reauthform {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .reautherr {
    flex-basis: 100%;
    color: var(--ember-lift);
    font-size: 12.5px;
  }
  .railbtn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    flex: none;
  }
  .nobundle {
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    color: var(--ember-lift);
    padding: 5px 11px;
    border-radius: var(--r-pill);
  }
  /* The badge is a control when an admin is holding the phone, and design.css's coarse block
     cannot reach it: that list names six primitives and a bare `<a>` is deliberately not among
     them, because adding it would grow every inline prose link. This is not an inline prose link
     — it is a bordered, padded, pill-radius badge in the header chrome, and on the screen a
     brand-new household spends its first session on it measured 137 by 25, barely half §6's
     floor on the axis a thumb needs. The scoped coarse rule is the shape `AccountChip`'s
     `.group a` already uses for the same reason; `inline-flex` because `min-height` does nothing
     to an inline box. [§6 preamble; M4.15 review cycle 1] */
  @media (pointer: coarse) {
    a.nobundle {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch);
    }
  }
  .body {
    flex: 1;
    min-height: 0;
    display: flex;
  }
  main {
    flex: 1;
    min-width: 0;
    overflow: auto;
    padding: 20px 22px 40px;
  }

  /* Phone-first: the rail becomes a bottom bar and the header keeps only identity. */
  @media (max-width: 720px) {
    .body {
      flex-direction: column-reverse;
    }
    main {
      padding: 14px 14px 24px;
    }
    /* The header now carries up to four items on a phone, and four do not fit. Measured on the
       e2e `phone` project's own device (iPhone 13, 390 px): brand 95 + trigger 96 + chip
       102-124 + the no-bundle badge's natural 112, against a 390 px row — the document scrolled
       sideways by 36-59 px, which is 06-responsive's rule and the classic phone failure.
       `gap: 10px` buys back the 18 px that keeps the ordinary state (a bundle imported, either
       household name measured) on one 54 px row; `flex-wrap` is what the four-badge state does
       instead of pushing the page sideways. The keyboard hint goes: it is not a claim worth
       making on a device with no keyboard. */
    /* And the inset again, on the property this block actually decides. M4.9 replaced the fixed
       height with `height: auto; min-height: 54px` so four badges wrap instead of pushing the
       page sideways — which silently discards the base rule's `calc(54px + env(...))` on exactly
       the form factor the inset exists for. The `padding-top` above still applies; this is the
       floor it has to clear. [decision 279] */
    header {
      gap: 10px;
      flex-wrap: wrap;
      height: auto;
      min-height: calc(54px + env(safe-area-inset-top));
    }
    .railkey {
      display: none;
    }
  }
</style>
