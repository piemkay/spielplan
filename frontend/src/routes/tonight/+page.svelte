<script>
  // One screen per step: door, lobby, round, waiting, ballot, reveal, solo. The pool is never
  // sent here, so nothing can draw it even by accident (§6.2 step 3).
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
  // The one runtime formatter, so a series reads `45m/ep` here too.
  import { metaLine } from '$lib/rate.svelte.js';

  let code = $state('');
  let sharpening = $state(false);
  let ending = $state(false);
  let disconnect = () => {};

  // The session this device's socket watches, re-pointed in one place so a racing tap cannot win.
  let watching = null;

  // A navigation away can land between `onMount`'s await and `watch`; this stops the late socket.
  let destroyed = false;

  function watch(sessionId) {
    if (destroyed) return;
    if (watching === sessionId && sessionId !== null) return;
    disconnect();
    watching = sessionId;
    disconnect = connect(sessionId);
  }

  onMount(async () => {
    // The slider opens where this member last left it for this kind (decision 506).
    restoreBudget(session.user?.id);
    // A reload or a backgrounded phone must not cost somebody their seat.
    const resumed = await bootstrap();
    // A `?room=` link lands in that room, then leaves the address so a reload does not rejoin.
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

  // Tonight payloads key the title `title_id`; `id` is set too so no other key names another title.
  const posterOf = (t) => (t ? { ...t, id: t.title_id } : null);

  // This member's own chips, and whose the rest are (decision 505).
  const vetoKeys = $derived(myVetoKeys(tonight.lobby, session.user?.id));
  const othersVetoes = $derived(othersVetoLines(tonight.lobby, session.user?.id));
  onDestroy(() => {
    destroyed = true;
    disconnect();
    // Stop the answer clock with the page: a tap out mid-pair must not be charged to the next answer.
    stopClock();
  });

  const isHost = $derived(
    !!tonight.lobby && tonight.lobby.host?.user_id === session.user?.id
  );
  // Guests answer on the host's phone once every earlier seat has finished (§6.2 step 2).
  const guestTurns = $derived(
    isHost
      ? (tonight.lobby?.seats ?? []).filter((s) => s.role === 'guest' && !s.ended_by)
      : []
  );
  // The store's list, so the round's and the ballot's hand-offs agree on whose phone this is.
  const ballotSeats = $derived(ballotTurns());
  // The seat the phone is holding, so Submit writes for that person rather than the owner.
  const ballotSeat = $derived(
    (tonight.lobby?.seats ?? []).find((s) => s.participant_id === tonight.activeSeat) ?? null
  );
  const ballotOpen = $derived(
    !!ballotSeat && !tonight.submittedSeats.includes(ballotSeat.participant_id)
  );

  // Back to the door; the seat is kept, and the rooms list offers resume.
  async function toDoor() {
    leave();
    // Stop watching the room too, or a session frame would re-read it.
    watch(null);
    sharpening = false;
    ending = false;
    await loadRooms();
  }

  // Two taps: ending is terminal and releases the room code.
  async function endTheRoom() {
    await endRoom();
    ending = false;
    watch(null);
    sharpening = false;
  }

  // Remember the budget when an evening is opened, not on every nudge.
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
      <!-- Back keeps the seat, so a restored room is never a trap. -->
      <button class="pill back" onclick={toDoor} data-testid="tonight-back">Back</button>
    {/if}
    <!-- In the header so every stuck state can reach it; host-only, and not on the reveal. -->
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
    <!-- Until the restore lands, a live door could open a second room for someone already seated. -->
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
        <!-- On a series night the number bounds minutes per episode, and this is where it is set. -->
        <span class="data" data-testid="tonight-budget-value"
          >{tonight.controls.runtime_budget_min} min{tonight.controls.kind === 'series'
            ? ' per episode'
            : ''}</span
        >
      </label>
      <!-- The budget is soft; said here, before the evening. -->
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
      <!-- Share opens the phone's sheet; without one the link is written out below. -->
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
      <!-- Up to three chips each; the pool leaves out anything anyone ruled out (decision 505). -->
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
      <!-- What to expect, not the cap, which the round is built to avoid. -->
      <p class="data label roundcount" data-testid="tonight-round-count">{roundHeader(tonight.round)}</p>
      <h2>Which one tonight?</h2>
      <!-- `.choice`, never `.poster`: design.css's global `.poster` is a 2:3 frame that filled the phone. -->
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
            {#each pairFacts(title) as fact, i (i)}
              <span class="why fact" data-testid={`tonight-pair-fact-${side}`}>{fact}</span>
            {/each}
          </button>
        {/each}
      </div>
      <!-- Side by side: stacked, the two answers cost the height the pair cards need. -->
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
        <!-- After this phone has voted, the ballot's status, not the round's counts. -->
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
        <!-- Whose ballot this is: on the host's phone it is not always the owner's. -->
        <p class="data label" data-testid="tonight-ballot-seat">{ballotSeat.name}</p>
        <!-- Chosen rows are outlined and ticked; the one filled control is Submit. -->
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
      <!-- The ballot's hand-off: without it a guest's vote could never be cast. -->
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
      <p class="data beat" data-testid="tonight-beat">{REVEAL_BEAT}</p>
      <div class="winner card" data-testid="tonight-winner">
        <!-- `.hero` bounds the poster so Play stays on a phone's first screen. -->
        <span class="hero"><RatePoster title={posterOf(tonight.result.winner)} showName={false} /></span>
        <h2>{tonight.result.winner?.name}</h2>
        <p class="why">{metaLine(tonight.result.winner)}</p>
        {#if tonight.result.winner?.label}
          <!-- The wildcard won: this card carries its label. -->
          <p class="why" data-testid="tonight-winner-label">{tonight.result.winner.label}</p>
        {/if}
        <p class="data" data-testid="tonight-approval-share">{approvalShare(tonight.result)}</p>
        {#if tonight.result.winner?.reserved}
          <!-- The reserved finalist is labelled as such (54d). -->
          <p class="data" data-testid="tonight-reserved">{RESERVED_LABEL}</p>
        {/if}
        {#if tonight.result.winner?.reserved_for}
          <!-- A seat's own pick carries its own label, never the counterweight's (decision 479). -->
          <p class="data" data-testid="tonight-reserved-for">
            {pickLabel(tonight.result.winner.reserved_for.name)}
          </p>
        {/if}
        <!-- How broad each yes was, released with the reveal (54e). -->
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
            <!-- A `const` keeps the row one text node; the labels follow the card wherever it lands. -->
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
              <!-- The label is the honesty (§6.4); approvals are said here once. -->
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
          <!-- Reshuffle posts `sharpen: false`, so no pair back is not a converged round: clear the flag. -->
          <button
            class="pill"
            onclick={() => {
              sharpening = false;
              loadSolo({ reshuffle: true });
            }}
            data-testid="tonight-reshuffle">Reshuffle</button
          >
          {#if !sharpening}
            <!-- Not gated on a pair in hand: this tap is what asks for one. -->
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
          <!-- A reshuffle that has come back round returns titles already seen here, so say so. -->
          <p class="why" data-testid="tonight-wrapped">{WRAPPED_LINE}</p>
        {/if}
        {#if sharpening && !tonight.solo.pair}
          <!-- Said out loud, or a converged round would answer "sharpen this" with a blank. -->
          <p class="why" data-testid="tonight-sharpen-done">
            The round has nothing left to ask — these picks are as sharp as this pool gets.
          </p>
        {/if}
        {#if tonight.solo.pair && sharpening}
          <!-- The same question as the round (§6.2 step 4), drawing both titles. -->
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
  /* One line, or Undo and the escape fall under the bottom bar. */
  .roundcount { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .why { color: var(--ink-3); font-size: 12.5px; line-height: 1.55; }
  .data { font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  /* design.css's full-width input rule outranks any selector here, but in a flex row the basis
     decides the main size. */
  .controls input[type='number'] { flex: 0 0 4.5rem; }
  /* The accent on the value the person chose, as on a pressed pill (§6.8). */
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
  .rooms ul, .seats, .slate, .picks, .matches, .reveal ul {
    list-style: none; margin: 0; padding: 0;
  }
  .rooms li, .seats li {
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 8px 0; border-bottom: 1px solid var(--line);
  }
  .seat { min-height: var(--touch); }
  /* Full ink, not the accent: the primary action on this step is Start (§6.8). */
  .code { font-size: 22px; letter-spacing: 0.18em; color: var(--ink); }
  /* Two columns at every width: stacked, the second option fell below the fold. */
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .choice {
    display: flex; flex-direction: column; gap: 6px; padding: 8px; min-width: 0;
    background: var(--card-raised);
    border: 1px solid var(--line-2); border-radius: var(--r-lg); color: var(--ink);
    text-align: left; cursor: pointer;
  }
  .choice:hover, .choice:focus-visible { border-color: var(--ember-edge); }
  /* Width bounds the 2:3 art, so the pair, the answers and Undo fit above an iPhone 13's bottom bar. */
  .art { display: block; width: min(100%, 13vh); align-self: center; }
  .choice .big {
    display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .choice .fact { line-height: 1.35; }
  /* Side by side and wrapping inside their half: still one 48px target each. */
  .levels { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .level { white-space: normal; line-height: 1.25; padding-inline: 12px; }
  .soft { margin: 0; }
  .thumb { display: block; flex: 0 0 44px; width: 44px; }
  /* Bounded like `.art`, so the winner's Play on Jellyfin stays on a phone's first screen. */
  .hero { display: block; width: min(100%, 16vh); }
  .link { word-break: break-all; }
  .vetoes { display: flex; flex-direction: column; gap: 8px; padding-top: 10px; }
  .vetoes p { margin: 0; }
  /* Outlined, not filled: the lobby's one filled control is Start. */
  .veto[aria-pressed='true'] {
    border-color: var(--ember-edge); background: var(--ember-wash); color: var(--ink);
  }
  .beat { letter-spacing: 0.2em; }
  .winner { border-color: var(--ember-edge); }
  .slate li { padding: 4px 0; }
  .option {
    display: flex; align-items: center; gap: 12px; width: 100%; min-height: var(--touch);
    padding: 6px 12px 6px 6px; background: var(--card-raised); color: var(--ink);
    border: 1px solid var(--line-2); border-radius: var(--r-md); text-align: left; cursor: pointer;
  }
  .option[aria-pressed='true'] { border-color: var(--ember-edge); background: var(--ember-wash); }
  .option-text { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
  .tick { flex: 0 0 18px; color: var(--ember-lift); font-size: 16px; }
  /* Sticky at the bottom of `main`, reaching through its end padding so no ballot row shows, or
     is tappable, beneath Submit. */
  .submitbar {
    position: sticky; bottom: calc(-1 * var(--main-pad-end, 0px)); z-index: 1;
    margin-top: 8px; padding: 8px 0 calc(8px + var(--main-pad-end, 0px));
    background: var(--card);
  }
  .submit { width: 100%; min-height: var(--touch); }
  .runner { display: flex; align-items: center; gap: 10px; padding: 4px 0; }
  .runner p { margin: 0; }
  .pick { display: flex; align-items: flex-start; gap: 12px; }
  .pick-text { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .picks { display: flex; flex-direction: column; gap: 10px; }
  .solo, .reveal, .round, .rooms { display: flex; flex-direction: column; gap: 12px; }
  .wildcard, .runners-up { display: flex; flex-direction: column; gap: 6px; }
  .wildcard p, .runners-up p { margin: 0; }
  .error { color: var(--ember-lift); font-size: 12.5px; }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .back { min-height: var(--touch); }
  /* On every pointer: a guest meets the hand-off first, and End must not be hit by accident. */
  .hand, .end { min-height: var(--touch); }
  .empty { color: var(--ink-2); font-size: 13px; }
  .disabled { opacity: 0.55; }
  /* An <a> and an aria-disabled <span>, which design.css's coarse floor does not reach. */
  .play {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: var(--touch); padding-inline: 20px;
  }
  @media (max-width: 560px) {
    .doors { grid-template-columns: 1fr; }
  }
</style>
