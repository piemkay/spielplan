<script>
  /**
   * Tonight. Spec v2.1 §6.2 as rewritten by the owner on 2026-08-29 (54a–54g), §6.7, §6.8.
   *
   * The surface is a state machine with one screen per step, because §6.2's steps are states a
   * household moves through together and a page that showed two at once would show one person
   * a screen another has left.
   *
   *   door    → the two doors (§6.2 step 1's controls sit above both), and the open-rooms list
   *   lobby   → the room: code, seats, and the host's Start
   *   round   → 54c's pairs, four answers, undo, and the escape from pair 6
   *   waiting → 54c's progress view: counts, never answers
   *   ballot  → 54e's blind approval multi-select
   *   reveal  → the beat, then the winner card
   *   solo    → 54f: three picks and a wildcard, no round first
   *
   * Nothing here draws the pool. §6.2 step 3 keeps it internal, and this component never
   * receives it — the anti-anchoring rule is a property of the payload, and the page is built
   * so that there is nothing to render even by accident.
   */
  import { onDestroy, onMount } from 'svelte';
  import { replaceState } from '$app/navigation';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { session } from '$lib/session.svelte.js';
  import {
    ANSWERS,
    BUDGET_DEFAULT,
    BUDGET_MAX,
    BUDGET_MIN,
    BUDGET_STEP,
    ESCAPE_LABEL,
    JOIN_CAPTION,
    MAX_GUESTS,
    MAX_VETOES,
    MOOD_CAPTION,
    RESERVED_LABEL,
    REVEAL_BEAT,
    SHARE_CAPTION,
    WRAPPED_LINE,
    answer,
    approvalShare,
    ballotTurns,
    ballotWaitingLine,
    bootstrap,
    breadthLine,
    budgetSoftLine,
    chooseKind,
    leave,
    connect,
    endRoom,
    escape,
    followLink,
    handBallot,
    join,
    linkedRoom,
    loadBallot,
    loadRooms,
    loadRound,
    loadSolo,
    myVetoKeys,
    onlyYesLines,
    openRoom,
    othersVetoLines,
    pairFacts,
    pickLabel,
    progressLine,
    rememberBudget,
    restoreBudget,
    roomLine,
    roundHeader,
    shareRoom,
    sharpen,
    start,
    stopClock,
    submitBallot,
    submitLabel,
    toggleApproval,
    toggleVeto,
    tonight,
    undo,
    vetoCaption
  } from '$lib/tonight.svelte.js';
  // The winner card's year-and-runtime line, from the one place that formats a runtime. This
  // screen spelled `{winner?.runtime_min} min` itself, so a series printed as flat minutes where
  // every other surface says `45m/ep`, a 170-minute film read `170 min` where the catalogue two
  // taps back said `2h 50m`, and a title of unknown runtime — nullable, and the corpus has them
  // — rendered a bare unit with no number. [M4.9 finding 37; review cycle 1: M49-CARD-2]
  import { metaLine } from '$lib/rate.svelte.js';

  let code = $state('');
  let sharpening = $state(false);
  let ending = $state(false);
  let disconnect = () => {};

  /** The session this device's socket is pointed at, so a re-point happens in one place rather
   * than three. `onMount` is async and a tap on "Together" can land before it resolves; the
   * loser used to overwrite the winner's subscription, leaving the device in the household
   * group and not the room's — household frames arrived and the room's own never did. */
  let watching = null;

  /** Set before `disconnect()` runs, and read by `watch` below. A navigation away can land
   * BETWEEN `onMount`'s await and its call to `watch`: `onDestroy` goes first, while
   * `disconnect` is still the no-op default above, and the continuation then opens a socket
   * into a closure nobody will ever call. Every such navigation leaked a live channel that went
   * on mutating `tonight.rooms` / `lobby` / `step` from a page that no longer exists — and the
   * only symptom is the surface behaving oddly somewhere else. [finding 19] */
  let destroyed = false;

  function watch(sessionId) {
    if (destroyed) return;
    if (watching === sessionId && sessionId !== null) return;
    disconnect();
    watching = sessionId;
    disconnect = connect(sessionId);
  }

  onMount(async () => {
    // The slider opens where this member last left it for this kind (decision 506): the second
    // household evening set 120, and the next visit opened at 130 again.
    restoreBudget(session.user?.id);
    // `bootstrap` returns the session this device is already seated in, if any — a reload, a
    // backgrounded phone or a navigation away and back must not cost somebody their evening
    // (§6.2 step 4 puts them on their own device for up to twenty pairs, and 54e's reveal
    // waits for every seat).
    const resumed = await bootstrap();
    // Decision 481: a `?room=` link — the lobby's Share, or the push invitation — lands in that
    // room. Taken off the address afterwards, so a reload of an evening that has since ended does
    // not try to join it again.
    const code = linkedRoom(location.search);
    const linked = code ? await followLink(code) : null;
    if (code) {
      try {
        replaceState(location.pathname, {});
      } catch {
        // Not under the router (a component test): the link stays in the bar, which is harmless.
      }
    }
    if (linked !== null) watch(linked);
    else if (watching === null) watch(resumed);
  });

  /** A title as the shared poster reads it. Every Tonight payload names the title `title_id`, which
   * `RatePoster` reads first since decision 483 (`lib/art.js`); the adapter also sets `id`, so no
   * other key a payload carries can name a different title. One adapter rather than a spread. */
  const posterOf = (t) => (t ? { ...t, id: t.title_id } : null);

  /** The chips THIS member has on, by key, and whose the rest of the room's are (decision 505). */
  const vetoKeys = $derived(myVetoKeys(tonight.lobby, session.user?.id));
  const othersVetoes = $derived(othersVetoLines(tonight.lobby, session.user?.id));
  onDestroy(() => {
    destroyed = true;
    disconnect();
    // The pair goes off the screen with the page, and §4.2's clock has to hear about it: the nav
    // rail renders over a live round, so one tap on Rank is an ordinary way out mid-pair, and the
    // module keeps its state while this component does not. The clock ran through the absence and
    // the next answer was charged it — a permanent, unmarked row in the column §14 risk 6 makes
    // the precondition for re-tuning the round. `loadRound` re-arms on the way back in, so the
    // answer after a remount measures the read the person actually gave the card.
    // [finding 41; M4.12 review cycle 1: M412-FE-1]
    stopClock();
  });

  // `me` used to live here and bound Submit to the viewer's own seat. `tonight.activeSeat`
  // replaces it on both hand-off screens, and the store's `refresh` is what keeps it pointed at
  // the right person — so a second derivation of "this device's seat" here would be a place for
  // the two to disagree. [findings 13, 14]
  const isHost = $derived(
    !!tonight.lobby && tonight.lobby.host?.user_id === session.user?.id
  );
  /** §6.2 step 2: guests answer on the initiator's phone, so the host's device offers their
   * turns once every earlier seat has finished. */
  const guestTurns = $derived(
    isHost
      ? (tonight.lobby?.seats ?? []).filter((s) => s.role === 'guest' && !s.ended_by)
      : []
  );
  /** The same hand-off one screen later, and the reason a room with any guest could never reach
   * 54e's reveal: the count the reveal waits on includes the guests, and Submit was bound to
   * the viewer's own seat. The list is the store's, so the round's turn and the ballot's turn
   * cannot disagree about whose phone this is. [finding 13] */
  const ballotSeats = $derived(ballotTurns());
  /** The seat whose ballot is on screen, and whether it still owes one. `activeSeat` is set by
   * `refresh` on arrival and by the hand-off after that, so Submit writes for the person
   * holding the phone rather than for its owner. */
  const ballotSeat = $derived(
    (tonight.lobby?.seats ?? []).find((s) => s.participant_id === tonight.activeSeat) ?? null
  );
  const ballotOpen = $derived(
    !!ballotSeat && !tonight.submittedSeats.includes(ballotSeat.participant_id)
  );

  /** Back to the door. The seat is kept — `resume` on the open-rooms row comes back to it. */
  async function toDoor() {
    leave();
    // And stop watching the room, not only its screen: a session-scoped frame ends in `refresh`,
    // which would re-read the room this device just stepped out of.
    watch(null);
    sharpening = false;
    ending = false;
    await loadRooms();
  }

  /** Decision 169's control. Two taps, because one mis-tap beside Back would end the
   * household's evening and there is no undo for it: `abandoned` is terminal, the room leaves
   * §6.2 step 2's list and its code is released. The room is stopped watching afterwards for
   * `toDoor`'s reason — a session frame would otherwise end in a `refresh` of a session this
   * device has just left. */
  async function endTheRoom() {
    await endRoom();
    ending = false;
    watch(null);
    sharpening = false;
  }

  /** The budget an evening is actually opened with is the one this device remembers for this
   * member and kind (decision 506) — on use rather than on every nudge of the slider. */
  function rememberControls() {
    rememberBudget(session.user?.id, tonight.controls.kind, tonight.controls.runtime_budget_min);
  }

  async function openAndWatch() {
    const room = await openRoom();
    if (room) {
      rememberControls();
      watch(room.session_id);
    }
  }

  async function joinAndWatch(args) {
    const joined = await join(args);
    if (joined) {
      code = '';
      watch(joined.session_id);
    }
  }
</script>

<section data-testid="tonight-surface">
  <header>
    <h1>Tonight</h1>
    {#if tonight.step !== 'door'}
      <!-- Stepping out is not leaving: the seat stays, and the open-rooms row for a room you
           are in is a `resume` control. Without this the restore that keeps a reload from
           stranding somebody becomes a trap of its own — one live room and the surface has no
           other door. -->
      <button class="pill back" onclick={toDoor} data-testid="tonight-back">Back</button>
    {/if}
    <!-- Decision 169. In the header rather than on one screen, because the states a room gets
         stuck in are several — a seat that never finishes leaves the host on `waiting`, a seat
         that never submits leaves them on 54e's ballot — and the remedy has to be reachable
         from whichever one the household is looking at. Host-only: §6.2 step 1 gives the host
         the session's controls, and a member ending the evening on the household's behalf is
         the same failure with the sign flipped. Not on the reveal: that room has `ended_at` set
         already, so the control could only ever produce `rooms.end_session`'s 404 — and an
         evening that resolved is not one that got stuck. -->
    {#if isHost && tonight.lobby && tonight.step !== 'door' && tonight.step !== 'reveal'}
      {#if ending}
        <button
          class="pill end on"
          onclick={endTheRoom}
          disabled={tonight.busy}
          data-testid="tonight-end-room-confirm">Yes, end it</button
        >
        <button class="pill end" onclick={() => (ending = false)} data-testid="tonight-end-room-cancel"
          >Keep going</button
        >
      {:else}
        <button class="pill end" onclick={() => (ending = true)} data-testid="tonight-end-room"
          >End room</button
        >
      {/if}
    {/if}
    {#if tonight.error}
      <p class="error" role="alert" data-testid="tonight-error">{tonight.error}</p>
    {/if}
  </header>

  {#if !tonight.booted}
    <!-- The restore is a round trip, so until it lands this device does not know whether it is
         at the door or in a room. Painting the door meanwhile is not a flicker: the controls are
         live, and a tap on "Together" in that window opens a second room for somebody who
         already has a seat in one. -->
    <p class="why" data-testid="tonight-booting">reading the room...</p>
  {:else if tonight.step === 'door'}
    <!-- §6.2 step 1: the controls sit before the solo/group fork and apply to both. -->
    <div class="controls card" data-testid="tonight-controls">
      <div class="row">
        <span class="data label">TYPE</span>
        {#each [['movie', 'Film'], ['series', 'Series']] as [value, label]}
          <button
            class="pill"
            aria-pressed={tonight.controls.kind === value}
            onclick={() => chooseKind(value, session.user?.id)}
            data-testid={`tonight-kind-${value}`}>{label}</button
          >
        {/each}
      </div>
      <label class="row">
        <span class="data label">TIME</span>
        <input
          type="range"
          min={BUDGET_MIN}
          max={BUDGET_MAX}
          step={BUDGET_STEP}
          bind:value={tonight.controls.runtime_budget_min}
          data-testid="tonight-budget"
        />
        <!-- 54h / decision 219: on a series night this number bounds minutes PER EPISODE, and
             this readout is where the household SETS it -- one row under the Series pill, and
             ahead of every label the server later attaches it to. The two labels the decision
             names report the number afterwards (a candidate's "fits your 60 min per episode",
             and the open-rooms row), so an unqualified "60 min" here is where the misreading
             starts: measured against the shipped bundle the series pool is 121 of 121 owned
             titles at 60, 130 and 200 alike, so the number narrows nothing and the evening's
             length is precisely what it is not. Spelled in place rather than through a shared
             constant, because `roomLine` spells its own the same way and two spellings of one
             word are cheaper than a module that owns it.
             [decision 219; M4.12 review cycle 2: M412-FE-5] -->
        <span class="data" data-testid="tonight-budget-value"
          >{tonight.controls.runtime_budget_min} min{tonight.controls.kind === 'series'
            ? ' per episode'
            : ''}</span
        >
      </label>
      <!-- §6.2 step 1's budget is soft, and this is where it is set: said here, before the
           evening, rather than discovered on a card that "runs 40 min over". -->
      <p class="why soft" data-testid="tonight-budget-soft">
        {budgetSoftLine(tonight.controls.kind)}
      </p>
      <label class="row">
        <span class="data label">REWATCHES</span>
        <input
          type="checkbox"
          bind:checked={tonight.controls.include_rewatches}
          data-testid="tonight-rewatches"
        />
        <span class="why"
          >{tonight.controls.include_rewatches
            ? 'including titles you have already seen'
            : 'skipping what everyone here has seen'}</span
        >
      </label>
      <label class="row">
        <span class="data label">GUESTS</span>
        <input
          type="number"
          min="0"
          max={MAX_GUESTS}
          bind:value={tonight.controls.guests}
          data-testid="tonight-guests"
        />
        <span class="why">they take their turns on this phone, after you</span>
      </label>
    </div>

    <div class="doors">
      <button class="door" onclick={openAndWatch} disabled={tonight.busy} data-testid="tonight-open">
        <span class="big">Together</span>
        <span class="why">a room the household can join</span>
      </button>
      <button
        class="door"
        onclick={() => {
          rememberControls();
          loadSolo();
        }}
        disabled={tonight.busy}
        data-testid="tonight-solo-door"
      >
        <span class="big">Just me</span>
        <span class="why">three picks and a wildcard, straight away</span>
      </button>
    </div>

    <div class="join card">
      <p class="data label">JOIN A ROOM</p>
      <form
        onsubmit={(e) => {
          e.preventDefault();
          joinAndWatch({ roomCode: code });
        }}
      >
        <input
          bind:value={code}
          placeholder="MX-2210"
          aria-label="room code"
          data-testid="tonight-code"
        />
        <button class="pill" type="submit" data-testid="tonight-join">Join</button>
      </form>
      <p class="why">{JOIN_CAPTION}</p>
    </div>

    <!-- §6.2 step 2: "active sessions are visible to every household device … with tappable
         empty seats". -->
    <div class="rooms card" data-testid="tonight-rooms">
      <p class="data label">OPEN ROOMS</p>
      {#if tonight.rooms.length === 0}
        <p class="why" data-testid="tonight-no-rooms">No room is open right now.</p>
      {:else}
        <ul>
          {#each tonight.rooms as room (room.session_id)}
            <li data-testid={`tonight-room-${room.room_code}`}>
              <span class="data">{roomLine(room)}</span>
              {#if room.joinable}
                <button
                  class="pill seat"
                  onclick={() => joinAndWatch({ sessionId: room.session_id })}
                  data-testid={`tonight-seat-${room.room_code}`}>tap to join</button
                >
              {:else if room.viewer_seated}
                <button
                  class="pill seat"
                  onclick={() => joinAndWatch({ sessionId: room.session_id })}
                  data-testid={`tonight-resume-${room.room_code}`}>resume</button
                >
              {:else}
                <span class="why">started</span>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}

  {#if tonight.step === 'lobby' && tonight.lobby}
    <div class="card lobby" data-testid="tonight-lobby">
      <p class="data code" data-testid="tonight-room-code">{tonight.lobby.room_code}</p>
      <!-- §6.2 step 2 names "room code / QR". The code ships and now its link does (decision
           481); the QR does not. A placeholder SVG once stood here claiming to be one, and this
           line then said "or send the link" with no link anywhere to send — the first household
           evening looked for it. Share opens the phone's own sheet, and where there is none the
           link is written out below so it can still be read aloud or pasted. -->
      <div class="row">
        <p class="why" data-testid="tonight-share-caption">{SHARE_CAPTION}</p>
        <button class="pill" onclick={shareRoom} data-testid="tonight-share">Share link</button>
      </div>
      {#if tonight.shareUrl}
        <p class="data link" data-testid="tonight-share-url">{tonight.shareUrl}</p>
      {/if}
      <p class="why">{JOIN_CAPTION}</p>
      <ul class="seats" data-testid="tonight-seats">
        {#each tonight.lobby.seats as seat (seat.participant_id)}
          <li>
            <span>{seat.name}</span>
            <span class="data label"
              >{seat.user_id === session.user?.id ? 'this phone' : seat.role}</span
            >
          </li>
        {/each}
      </ul>
      <!-- Decision 480's "not tonight": any seated member, before Start. Up to three EACH, and
           the pool leaves out everything anyone ruled out (decision 505) — the chips are this
           phone's own, and the lines under them say whose the others are. A title goes if it
           may carry the term in either tier, quoted or inferred (decision 504), which is what the
           caption's "may contain" is honest about. -->
      <div class="vetoes" data-testid="tonight-vetoes">
        <p class="data label">NOT TONIGHT</p>
        <div class="row">
          {#each tonight.lobby.veto_options ?? [] as option (option.key)}
            {@const on = vetoKeys.includes(option.key)}
            <button
              class="pill veto"
              aria-pressed={on}
              disabled={tonight.busy || (!on && vetoKeys.length >= MAX_VETOES)}
              onclick={() => toggleVeto(option.key, session.user?.id)}
              data-testid={`tonight-veto-${option.key}`}>{option.label}</button
            >
          {/each}
        </div>
        {#each othersVetoes as line (line)}
          <p class="data" data-testid="tonight-others-vetoes">{line}</p>
        {/each}
        <p class="why" data-testid="tonight-veto-caption">{vetoCaption(tonight.lobby.kind)}</p>
        <!-- The mood the second household evening asked for a control to say: the answers carry
             it already, so the lobby says how rather than adding a question (§0's stored-mood
             measurement; decision 480 keeps the mood round deleted). -->
        <p class="why" data-testid="tonight-mood-caption">{MOOD_CAPTION}</p>
      </div>
      {#if isHost}
        <p class="why">Start whenever you are ready. Anyone who joins before you start is in.</p>
        <button
          class="pill on"
          onclick={start}
          disabled={tonight.busy}
          data-testid="tonight-start">Start</button
        >
      {:else}
        <p class="why" data-testid="tonight-waiting-for-host">
          {tonight.lobby.host?.name} starts when everyone is in.
        </p>
      {/if}
    </div>
  {/if}

  {#if tonight.step === 'round' && tonight.round?.pair}
    <div class="round" data-testid="tonight-round">
      <!-- What to expect, not the cap: "pair 1 · cap 20" read as the plan for the evening, when
           the cap is the ending the round is built to avoid. `roundHeader` adds it back near it. -->
      <p class="data label roundcount" data-testid="tonight-round-count">{roundHeader(tonight.round)}</p>
      <h2>Which one tonight?</h2>
      <!-- `.choice`, never `.poster`: design.css's global `.poster` is a 2:3 frame, and a button
           wearing it was a screen-high box on an iPhone 13 — the second option and every answer
           below the fold (the first household evening's screenshot). The frame belongs to the
           shared poster inside, bounded in height, and the pair stays two columns on a phone the
           way the Rate battle card does. -->
      <div class="pair">
        {#each [['A', tonight.round.pair.a], ['B', tonight.round.pair.b]] as [side, title]}
          <button
            class="choice"
            onclick={() => answer(side)}
            disabled={tonight.busy}
            data-testid={`tonight-pick-${side}`}
          >
            <span class="art"><RatePoster title={posterOf(title)} showName={false} /></span>
            <span class="big">{title?.name}</span>
            <!-- What the title is, for somebody who does not know it: year, runtime, how far
                 over the budget it runs if it does, and its genres in plain words. -->
            {#each pairFacts(title) as fact, i (i)}
              <span class="why fact" data-testid={`tonight-pair-fact-${side}`}>{fact}</span>
            {/each}
          </button>
        {/each}
      </div>
      <!-- The two level answers side by side, each half the width and allowed to wrap: stacked,
           they cost the height the pair cards' genre line needs to keep both options and every
           answer above an iPhone 13's bottom bar. -->
      <div class="levels">
        {#each ANSWERS.filter((a) => a.value === 'EITHER' || a.value === 'NEITHER') as choice}
          <button
            class="pill level"
            onclick={() => answer(choice.value)}
            disabled={tonight.busy}
            data-testid={`tonight-answer-${choice.value}`}>{choice.label}</button
          >
        {/each}
      </div>
      <div class="row quiet">
        <button class="pill" onclick={undo} data-testid="tonight-undo">Undo</button>
        {#if tonight.round.escape_available}
          <button class="pill" onclick={escape} data-testid="tonight-escape">{ESCAPE_LABEL}</button>
        {:else}
          <span class="why" data-testid="tonight-escape-locked"
            >“{ESCAPE_LABEL}” opens at pair 6</span
          >
        {/if}
      </div>
    </div>
  {/if}

  {#if tonight.step === 'waiting'}
    <div class="card" data-testid="tonight-waiting">
      <p class="data label">WAITING</p>
      {#if tonight.lobby?.state === 'ballot'}
        <!-- After this phone has voted, the ballot's own status. It showed the ROUND's counts
             ("Patrick 1/1 done · Jenny 6/6 done") under a ballot that was still waiting. -->
        <p class="data" data-testid="tonight-ballot-waiting">{ballotWaitingLine(tonight.ballot)}</p>
        <p class="why">Nobody sees anybody's votes until every vote is in.</p>
      {:else}
        <p class="data" data-testid="tonight-progress">{progressLine(tonight.progress)}</p>
        <p class="why">Nobody sees anybody's answers until every round has finished.</p>
        {#each guestTurns as guest (guest.participant_id)}
          <button
            class="pill"
            onclick={() => loadRound(guest.participant_id)}
            data-testid={`tonight-hand-to-${guest.participant_id}`}>pass to {guest.name}</button
          >
        {/each}
      {/if}
    </div>
  {/if}

  {#if tonight.step === 'ballot' && tonight.ballot?.slate}
    <div class="card" data-testid="tonight-ballot">
      <h2>Tap what you'd be happy with</h2>
      <p class="why">Approvals stay hidden until everyone has submitted.</p>
      {#if ballotOpen}
        <!-- Whose ballot this is. On the initiator's phone it is not always its owner's, and a
             person handed a phone has to be told which vote they are casting before they cast
             it — the screen is otherwise identical for every seat. -->
        <p class="data label" data-testid="tonight-ballot-seat">{ballotSeat.name}</p>
        <!-- Full-width rows with a tick, and Submit a different object entirely. The options were
             ember pills and Submit was an ember pill (`.pill.on` and `.pill[aria-pressed]` are
             one rule in design.css), so four chosen titles and the button that casts them read as
             five of the same thing. A chosen row is outlined and ticked; the one filled control
             on the screen is the one that submits, and it says what it will cast. -->
        <ul class="slate">
          {#each tonight.ballot.slate as card (card.title_id)}
            {@const picked = tonight.approved.includes(card.title_id)}
            <li>
              <button
                class="option"
                aria-pressed={picked}
                onclick={() => toggleApproval(card.title_id)}
                data-testid={`tonight-approve-${card.title_id}`}
              >
                <span class="thumb"><RatePoster title={posterOf(card)} showName={false} /></span>
                <span class="option-text">
                  <span class="big">{card.name}</span>
                  {#if card.slot === 'wildcard'}<span class="why">a step outside your usual</span>{/if}
                </span>
                <span class="tick" aria-hidden="true">{picked ? '✓' : ''}</span>
              </button>
            </li>
          {/each}
        </ul>
        <div class="submitbar" data-testid="tonight-submit-bar">
          <button
            class="btn-primary submit"
            onclick={() => submitBallot(tonight.activeSeat)}
            disabled={tonight.busy || tonight.activeSeat === null}
            data-testid="tonight-submit-ballot">{submitLabel(tonight.approved.length)}</button
          >
        </div>
      {/if}
      <!-- The round's hand-off (the waiting screen's `tonight-hand-to-` control), one screen
           later and with the stakes of the whole evening. Without it the phone could carry a
           guest through twenty pairs and then had no way to cast their vote, and
           `ballot.submitted_count` counts them — so the reveal waited on a ballot no screen
           could submit and the evening never ended. `handBallot` clears the previous person's
           ticks, which is 54e's blindness across the hand-off rather than only across the
           room. [finding 13] -->
      {#each ballotSeats as guest (guest.participant_id)}
        <button
          class="pill hand"
          onclick={() => handBallot(guest.participant_id)}
          data-testid={`tonight-ballot-to-${guest.participant_id}`}>pass to {guest.name}</button
        >
      {/each}
      <p class="data" data-testid="tonight-ballot-progress">
        {tonight.ballot.submitted} of {tonight.ballot.seated} submitted
      </p>
    </div>
  {/if}

  {#if tonight.step === 'reveal' && tonight.result}
    <div class="reveal" data-testid="tonight-reveal">
      <!-- proposal 60: the beat comes before the winner. "Shipping the property without the
           moment ships half of it." -->
      <p class="data beat" data-testid="tonight-beat">{REVEAL_BEAT}</p>
      <div class="winner card" data-testid="tonight-winner">
        <!-- The winner's poster (decision 483), the card's first child. The payload is keyed
             `title_id`, which RatePoster reads first; `.hero` bounds it so Play on Jellyfin stays
             on a phone's first screen. After the reveal, so no anchoring rule applies. -->
        <span class="hero"><RatePoster title={posterOf(tonight.result.winner)} showName={false} /></span>
        <h2>{tonight.result.winner?.name}</h2>
        <!-- 54h's per-episode qualifier arrives here for free, and that is worth saying rather
             than re-deriving: `metaLine` reads the card's `kind` and prints `24m/ep` for a
             series, and the reveal card carries the session's kind since M4.9. The label the
             winner card was missing under decision 219 is `fit_line`'s, and the server builds
             that one. [decision 219] -->
        <p class="why">{metaLine(tonight.result.winner)}</p>
        {#if tonight.result.winner?.label}
          <!-- The wildcard won: this card is its one place on the reveal, so it carries the
               honest label the wildcard block would have. -->
          <p class="why" data-testid="tonight-winner-label">{tonight.result.winner.label}</p>
        {/if}
        <p class="data" data-testid="tonight-approval-share">{approvalShare(tonight.result)}</p>
        {#if tonight.result.winner?.reserved}
          <!-- 54d: the reserved finalist is "labelled as such". The household is told "here's
               one of each" by the conflict line below; this is the half that says which card is
               the other each, and without it the clause had no implementation at all. Inert on
               the shipped bundle — decision 173 ships no axes, so nothing is reserved on real
               data — but the rule is the rule. [decision 220] -->
          <p class="data" data-testid="tonight-reserved">{RESERVED_LABEL}</p>
        {/if}
        {#if tonight.result.winner?.reserved_for}
          <!-- Decision 479: a seat's own pick, placed because none of their top three was on the
               plain ranking. Its own label, never the axis counterweight's. -->
          <p class="data" data-testid="tonight-reserved-for">
            {pickLabel(tonight.result.winner.reserved_for.name)}
          </p>
        {/if}
        <!-- How broad each yes was, instead of "Unanimous." — which was true over an evening where
             one member approved four and the other one, the winner being her only yes. Released
             with the reveal and not before (54e). -->
        <p class="why" data-testid="tonight-breadth">{breadthLine(tonight.result)}</p>
        {#each onlyYesLines(tonight.result) as only (only)}
          <p class="why" data-testid="tonight-only-yes">{only}</p>
        {/each}
        <p class="why" data-testid="tonight-fit-line">{tonight.result.winner?.fit_line}</p>
        <ul class="matches" data-testid="tonight-match-lines">
          {#each tonight.result.winner?.match_lines ?? [] as line}
            <li class="why">{line.line}</li>
          {/each}
        </ul>
        {#if tonight.result.winner?.conflict}
          <p class="why" data-testid="tonight-conflict">
            {tonight.result.winner.conflict.headline}
            {tonight.result.winner.conflict.explanation}
          </p>
        {/if}
        {#if tonight.result.winner?.play_url}
          <a
            class="btn-primary play"
            href={tonight.result.winner.play_url}
            data-testid="tonight-play">Play on Jellyfin</a
          >
        {:else}
          <span class="pill disabled play" aria-disabled="true" data-testid="tonight-play"
            >Play on Jellyfin — no Jellyfin link</span
          >
        {/if}
      </div>

      <div class="runners-up card" data-testid="tonight-runners-up">
        <p class="data label">RUNNERS-UP</p>
        <ul>
          {#each tonight.result.runners_up ?? [] as card (card.title_id)}
            <!-- The reservation is a claim about the SLATE, so the label follows the card
                 wherever it lands: the counterweight is a finalist and the votes may leave it
                 here. Built as a `const` so the row's text stays one node — Svelte collapses the
                 whitespace around an expression that spans lines, and this row is read by the
                 eye as one sentence. [decision 220] A seat's own pick follows its card the same
                 way (decision 479). -->
            {@const counterweight = card.reserved
              ? ` · ${RESERVED_LABEL}`
              : card.reserved_for
                ? ` · ${pickLabel(card.reserved_for.name)}`
                : ''}
            <li class="runner">
              <span class="thumb"><RatePoster title={posterOf(card)} showName={false} /></span>
              <span class="why" data-testid={`tonight-runner-up-${card.title_id}`}
                >{card.name} · {card.approvals} approved{counterweight}</span
              >
            </li>
          {/each}
          {#if (tonight.result.runners_up ?? []).length === 0}
            <li class="why">nothing else was in the running</li>
          {/if}
        </ul>
      </div>

      {#if tonight.result.wildcard}
        <div class="wildcard card" data-testid="tonight-wildcard">
          <p class="data label">WILDCARD</p>
          <div class="runner">
            <span class="thumb"><RatePoster title={posterOf(tonight.result.wildcard)} showName={false} /></span>
            <div>
              <p>{tonight.result.wildcard.name}</p>
              <!-- §6.4's "honestly labelled" is served, not spelled here: the words are the rule,
                   and the label IS the honesty, so the screen does not also announce that it is
                   being honest. Its approvals are said here, once — the runners-up no longer list
                   it a second time. -->
              <p class="why" data-testid="tonight-wildcard-line">
                {`${tonight.result.wildcard.label} · ${tonight.result.wildcard.approvals ?? 0} approved`}
              </p>
            </div>
          </div>
        </div>
      {/if}
    </div>
  {/if}

  {#if tonight.step === 'solo' && tonight.solo}
    <div class="solo" data-testid="tonight-solo">
      <p class="data" data-testid="tonight-provenance">{tonight.solo.provenance}</p>
      {#if tonight.solo.empty}
        <p class="empty" data-testid="tonight-solo-empty">{tonight.solo.empty}</p>
      {:else}
        <ul class="picks" data-testid="tonight-picks">
          {#each tonight.solo.picks as pick (pick.title_id)}
            <li class="card pick" data-testid={`tonight-pick-${pick.title_id}`}>
              <span class="thumb"><RatePoster title={posterOf(pick)} showName={false} /></span>
              <span class="pick-text">
                <span class="big">{pick.name}</span>
                <span class="why">{pick.why}</span>
                <span class="data">{pick.fit_line}</span>
              </span>
            </li>
          {/each}
        </ul>
        {#if tonight.solo.wildcard}
          <div class="card pick" data-testid="tonight-solo-wildcard">
            <span class="thumb"><RatePoster title={posterOf(tonight.solo.wildcard)} showName={false} /></span>
            <span class="pick-text">
              <span class="big">{tonight.solo.wildcard.name}</span>
              <span class="why">{tonight.solo.wildcard.why}</span>
              <span class="data">{tonight.solo.wildcard.fit_line}</span>
            </span>
          </div>
        {/if}
        <div class="row">
          <!-- The walk leaves the round, and the flag has to leave with it. Reshuffle is a browse
               gesture, so it posts `sharpen: false` and the server sends no pair back — it was
               not asked for one, and a skipped selection is `pair: null` with `stop_reason` null,
               which on the wire is the converged round it is not. Held here rather than read off
               `stop_reason`, because the screen's question is which gesture it is showing the
               result of: with the flag left standing, the picks came back under "nothing left to
               ask" and the one control that could ask for a pair was hidden, so Back was the only
               way out and Back clears the evening's sharpen answers.
               [decision 222; M4.12 review cycle 1: M412-SOLO-01] -->
          <button
            class="pill"
            onclick={() => {
              sharpening = false;
              loadSolo({ reshuffle: true });
            }}
            data-testid="tonight-reshuffle">Reshuffle</button
          >
          {#if !sharpening}
            <!-- No longer gated on a pair being in hand: 54f's door lands on the picks and the
                 pair search is what this tap ASKS for, so the pair arrives with the tap rather
                 than ahead of it. Gating on `solo.pair` while the door stopped drawing one would
                 have hidden the control that is the only way to get one. -->
            <button
              class="pill"
              onclick={() => {
                sharpening = true;
                loadSolo({ sharpen: true });
              }}
              data-testid="tonight-sharpen">sharpen this</button
            >
          {/if}
        </div>
        {#if tonight.solo.wrapped}
          <!-- 54f: the reshuffle "walks further down the ranking", and a walk that has come back
               round returns titles this person has already seen on this screen. The flag has
               been on the payload since M4 with no reader, so a fourth press on a small pool
               looked broken. [decision 222] -->
          <p class="why" data-testid="tonight-wrapped">{WRAPPED_LINE}</p>
        {/if}
        {#if sharpening && !tonight.solo.pair}
          <!-- The round has nothing left to ask: 54c's shortlist resolved, or the cap was
               reached. Said out loud because the tap that asks for a pair is now the tap that
               STARTS the round, so a pool that converges at zero answers would otherwise answer
               "sharpen this" with a blank space where the question should be — §6.8's register
               is a surface that says what it did. -->
          <p class="why" data-testid="tonight-sharpen-done">
            The round has nothing left to ask — these picks are as sharp as this pool gets.
          </p>
        {/if}
        {#if tonight.solo.pair && sharpening}
          <!-- 54f runs "the same adaptive round against the same pool", which means the same
               question: §6.2 step 4's "Which one tonight?". An earlier version had one button
               that posted `A` without drawing either title, so every tap recorded a preference
               nobody had expressed and then re-ranked the picks by it. -->
          <div class="round" data-testid="tonight-sharpen-pair">
            <h2>Which one tonight?</h2>
            <div class="pair">
              {#each [['A', tonight.solo.pair.a], ['B', tonight.solo.pair.b]] as [side, title]}
                <button
                  class="choice"
                  onclick={() => sharpen(side)}
                  disabled={tonight.busy}
                  data-testid={`tonight-sharpen-${side}`}
                >
                  <span class="art"><RatePoster title={posterOf(title)} showName={false} /></span>
                  <span class="big">{title?.name}</span>
                  {#each pairFacts(title) as fact, i (i)}
                    <span class="why fact">{fact}</span>
                  {/each}
                </button>
              {/each}
            </div>
            <div class="levels">
              {#each ANSWERS.filter((a) => a.value !== 'A' && a.value !== 'B') as choice}
                <button
                  class="pill level"
                  onclick={() => sharpen(choice.value)}
                  disabled={tonight.busy}
                  data-testid={`tonight-sharpen-${choice.value}`}>{choice.label}</button
                >
              {/each}
            </div>
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</section>

<style>
  section { display: flex; flex-direction: column; gap: 16px; max-width: 62ch; }
  h1 { margin: 0; font-size: 21px; font-weight: 600; }
  h2 { margin: 0; font-size: 17px; font-weight: 600; }
  .label { letter-spacing: 0.14em; color: var(--ink-4); font-size: 10px; }
  /* One line whatever the round says, for `roundHeader`'s reason: a second line put Undo and the
     escape under the bottom bar (review finding UX-4). */
  .roundcount { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .why { color: var(--ink-3); font-size: 12.5px; line-height: 1.55; }
  .data { font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  /* design.css gives every input `width: 100%` except checkbox, radio, range and file. A number
     stepper in a row is the case that list does not cover: at full width it pushed the hint onto
     a line of its own and made a one-line control three lines tall.
     `flex-basis` rather than `width`, because that rule's four `:not()`s make it specificity
     0-4-1 and no reasonable selector here outranks it — but the row is a flex container, so the
     basis decides the main size and `width` never gets a say. */
  .controls input[type='number'] { flex: 0 0 4.5rem; }
  /* The one range control in the app. Left to the user agent it draws in the platform's blue,
     which is the one colour §6.8's surface does not otherwise contain. This is the accent under
     its own rule rather than beside it: `accent-color` paints the part of a native control that
     shows the value the person has chosen — the same grammar as `.pill[aria-pressed='true']`,
     which is why it stays while the six fills around it go. [§6.8; decision 277] */
  .controls input[type='range'] { accent-color: var(--ember); }
  .quiet { opacity: 0.85; }
  .controls { display: flex; flex-direction: column; gap: 10px; }
  .doors { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .door {
    min-height: var(--touch);
    display: flex; flex-direction: column; gap: 4px; padding: 18px 14px;
    background: var(--card); border: 1px solid var(--line-2); border-radius: var(--r-lg);
    color: var(--ink); text-align: left; cursor: pointer;
  }
  .door:hover, .door:focus-visible { border-color: var(--ember-edge); }
  .big { font-size: 16px; font-weight: 600; }
  .join form { display: flex; gap: 8px; }
  .join input { min-height: var(--touch); flex: 1; }
  /* Every list on this surface is reset, and the runners-up list was the one that was not:
     it rendered with the user agent's bullets and indent beside three sibling blocks that had
     neither. */
  .rooms ul, .seats, .slate, .picks, .matches, .reveal ul {
    list-style: none; margin: 0; padding: 0;
  }
  .rooms li, .seats li {
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 8px 0; border-bottom: 1px solid var(--line);
  }
  .seat { min-height: var(--touch); }
  /* The room code is the lobby's content, not its accent: the host reads it out and the guests
     type it, so it wants the full ink the rest of the data voice steps down from. It wore the
     ember, which §6.8 spends on selection and primary actions — and the primary action on this
     step is Start. [§6.8; decision 276] */
  .code { font-size: 22px; letter-spacing: 0.18em; color: var(--ink); }
  /* Two columns at every width, like the Rate battle card: a pair is a comparison, and stacked on a
     phone it put the second option and the answers below the fold. */
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .choice {
    display: flex; flex-direction: column; gap: 6px; padding: 8px; min-width: 0;
    background: var(--card-raised);
    border: 1px solid var(--line-2); border-radius: var(--r-lg); color: var(--ink);
    text-align: left; cursor: pointer;
  }
  .choice:hover, .choice:focus-visible { border-color: var(--ember-edge); }
  /* The art's WIDTH is what bounds it, because the shared poster is `width: 100%` inside a 2:3
     frame: 13vh wide is at most 19.5vh tall, and the title is held to two lines, so on an iPhone
     13's 664 px the pair, the two level answers and Undo all sit above the bottom bar (measured
     on the fixture bundle at 390 x 664 with two-line titles, an over-budget line and genres on
     both cards: Undo ends at 602 of the bar's 603). */
  .art { display: block; width: min(100%, 13vh); align-self: center; }
  .choice .big {
    display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  /* The facts under a pair card's name sit tight under it, one short line each. */
  .choice .fact { line-height: 1.35; }
  /* Decision 154's two level answers, side by side and allowed to wrap inside their half: the
     longer label is wider than half a phone, and a pill that wraps to two lines is still one
     48 px target where stacking them cost a whole row. */
  .levels { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .level { white-space: normal; line-height: 1.25; padding-inline: 12px; }
  .soft { margin: 0; }
  .thumb { display: block; flex: 0 0 44px; width: 44px; }
  /* Bounded like `.art`, so the winner's Play on Jellyfin stays on a phone's first screen. */
  .hero { display: block; width: min(100%, 16vh); }
  .link { word-break: break-all; }
  .vetoes { display: flex; flex-direction: column; gap: 8px; padding-top: 10px; }
  .vetoes p { margin: 0; }
  /* An on veto is outlined, not filled, for the ballot's reason: the lobby's one filled control
     is Start, and a filled "violence" beside it read as a second thing to press to begin. */
  .veto[aria-pressed='true'] {
    border-color: var(--ember-edge); background: var(--ember-wash); color: var(--ink);
  }
  /* Proposal 60's beat is a label over the reveal, and nothing here is chosen yet — so it keeps
     the data voice's own `--ink-2` (this file's `.data`, one rule up) and spends no accent. The
     letter-spacing is what makes it a beat. [§6.8; decision 276] */
  .beat { letter-spacing: 0.2em; }
  .winner { border-color: var(--ember-edge); }
  /* `.slate` rows are plain list items; `.picks` rows are cards, so they take the card's own
     padding rather than a bare vertical rhythm. Sharing one rule left them with no horizontal
     padding at all, text starting on the border. */
  .slate li { padding: 4px 0; }
  /* A ballot option: a full-width row, outlined and ticked when chosen. Deliberately not the
     filled ember of `.pill[aria-pressed]`, which is the same rule as `.pill.on` and made four
     chosen titles look exactly like the Submit beneath them; the fill is Submit's alone. */
  .option {
    display: flex; align-items: center; gap: 12px; width: 100%; min-height: var(--touch);
    padding: 6px 12px 6px 6px; background: var(--card-raised); color: var(--ink);
    border: 1px solid var(--line-2); border-radius: var(--r-md); text-align: left; cursor: pointer;
  }
  .option[aria-pressed='true'] { border-color: var(--ember-edge); background: var(--ember-wash); }
  .option-text { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
  .tick { flex: 0 0 18px; color: var(--ember-lift); font-size: 16px; }
  /* Held at the bottom of the scroll area while the options run on below it: four rows put
     Submit under the fold on an iPhone 13 (the second household evening), and the one filled
     control on the screen is the one that must always be in reach. `main` is the scroll
     container and ends above the bottom bar, so a sticky bottom sits on the bar and not under
     it.
     An opaque footer and not a floating button: a floating Submit 8 px up left the last option's
     second line showing, and tappable, in the 32 px under it - a near miss approved a title whose
     name Submit covered (review finding UX-2). The footer reaches through `main`'s end padding
     (`--main-pad-end`, where sticky offsets stop) to the scrollport's edge, so nothing of the
     ballot shows beneath it, and keeps the button where it stood. */
  .submitbar {
    position: sticky; bottom: calc(-1 * var(--main-pad-end, 0px)); z-index: 1;
    margin-top: 8px; padding: 8px 0 calc(8px + var(--main-pad-end, 0px));
    background: var(--card);
  }
  .submit { width: 100%; min-height: var(--touch); }
  .runner { display: flex; align-items: center; gap: 10px; padding: 4px 0; }
  .runner p { margin: 0; }
  /* The pick cards and the wildcard beside them are the same object and lay out the same way:
     the shared poster on the left and the three lines beside it in a column. Only the list items
     had the column rule once, so the wildcard's spans ran together on one line. */
  .pick { display: flex; align-items: flex-start; gap: 12px; }
  .pick-text { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  /* Cards in a stack need a gap or their borders meet and read as one box with rules across it. */
  .picks { display: flex; flex-direction: column; gap: 10px; }
  /* Every step wrapper is a column of blocks and none of them said so, so each one's children
     sat flush against each other in normal flow while `section`'s own gap spaced the steps. */
  .solo, .reveal, .round, .rooms { display: flex; flex-direction: column; gap: 12px; }
  .wildcard, .runners-up { display: flex; flex-direction: column; gap: 6px; }
  .wildcard p, .runners-up p { margin: 0; }
  .error { color: var(--ember-lift); font-size: 12.5px; }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .back { min-height: var(--touch); }
  /* §6 preamble's 48 px floor on the two controls this milestone adds. design.css raises
     `button.pill` on a coarse pointer, which is the phone — these say it on every pointer,
     because the hand-off is the control a guest meets first and the end control is the one that
     must not be hit by accident. */
  .hand, .end { min-height: var(--touch); }
  .empty { color: var(--ink-2); font-size: 13px; }
  .disabled { opacity: 0.55; }
  /* §6 preamble's 48 px floor. design.css raises `button.pill` on a coarse pointer; the Play
     CTA is an <a> (§7.1's deep link) and an aria-disabled <span>, so neither is reached by it. */
  .play {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: var(--touch); padding-inline: 20px;
  }
  @media (max-width: 560px) {
    .doors { grid-template-columns: 1fr; }
  }
</style>
