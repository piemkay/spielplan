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
    REVEAL_BEAT,
    answer,
    approvalShare,
    bootstrap,
    leave,
    connect,
    escape,
    join,
    loadBallot,
    loadRooms,
    loadRound,
    loadSolo,
    openRoom,
    progressLine,
    roomLine,
    sharpen,
    start,
    submitBallot,
    toggleApproval,
    tonight,
    undo
  } from '$lib/tonight.svelte.js';

  let code = $state('');
  let sharpening = $state(false);
  let disconnect = () => {};

  /** The session this device's socket is pointed at, so a re-point happens in one place rather
   * than three. `onMount` is async and a tap on "Together" can land before it resolves; the
   * loser used to overwrite the winner's subscription, leaving the device in the household
   * group and not the room's — household frames arrived and the room's own never did. */
  let watching = null;

  function watch(sessionId) {
    if (watching === sessionId && sessionId !== null) return;
    disconnect();
    watching = sessionId;
    disconnect = connect(sessionId);
  }

  onMount(async () => {
    // `bootstrap` returns the session this device is already seated in, if any — a reload, a
    // backgrounded phone or a navigation away and back must not cost somebody their evening
    // (§6.2 step 4 puts them on their own device for up to twenty pairs, and 54e's reveal
    // waits for every seat).
    const resumed = await bootstrap();
    if (watching === null) watch(resumed);
  });
  onDestroy(() => disconnect());

  const me = $derived(tonight.lobby?.me ?? null);
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

  /** Back to the door. The seat is kept — `resume` on the open-rooms row comes back to it. */
  async function toDoor() {
    leave();
    // And stop watching the room, not only its screen: a session-scoped frame ends in `refresh`,
    // which would re-read the room this device just stepped out of.
    watch(null);
    sharpening = false;
    await loadRooms();
  }

  async function openAndWatch() {
    const room = await openRoom();
    if (room) watch(room.session_id);
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
            onclick={() => (tonight.controls.kind = value)}
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
        <span class="data" data-testid="tonight-budget-value"
          >{tonight.controls.runtime_budget_min} min</span
        >
      </label>
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
        onclick={() => loadSolo()}
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
      <!-- §6.2 step 2 names "room code / QR". The code ships; the QR does not.
           A placeholder SVG stood here with an alt that told a screen-reader user it was a QR
           and a comment claiming it encoded PUBLIC_URL — it was a rectangle with the code
           written in it, and the review caught the lie. A real encoder is a day's work with
           its own tests and no dependency is permitted to bring one in, so the honest thing is
           to ship the channel that works and say the other is not here yet. Recorded in
           M4-open-points. -->
      <p class="why" data-testid="tonight-no-qr">
        Read the code out, or send the link — the QR arrives later.
      </p>
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
      <p class="data label" data-testid="tonight-round-count">
        pair {tonight.round.answered + 1} · cap {tonight.round.cap}
      </p>
      <h2>Which one tonight?</h2>
      <div class="pair">
        {#each [['A', tonight.round.pair.a], ['B', tonight.round.pair.b]] as [side, title]}
          <button
            class="poster"
            onclick={() => answer(side)}
            disabled={tonight.busy}
            data-testid={`tonight-pick-${side}`}
          >
            <span class="big">{title?.name}</span>
            <span class="why">{title?.year} · {title?.fit_line}</span>
          </button>
        {/each}
      </div>
      <div class="row">
        {#each ANSWERS.filter((a) => a.value === 'EITHER' || a.value === 'NEITHER') as choice}
          <button
            class="pill"
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
      {#if tonight.rail.length}
        <ul class="rail" data-testid="tonight-rail">
          {#each tonight.rail as event (event.id)}<li class="data">{event.text}</li>{/each}
        </ul>
      {/if}
    </div>
  {/if}

  {#if tonight.step === 'waiting'}
    <div class="card" data-testid="tonight-waiting">
      <p class="data label">WAITING</p>
      <p class="data" data-testid="tonight-progress">{progressLine(tonight.progress)}</p>
      <p class="why">Nobody sees anybody's answers until every round has finished.</p>
      {#each guestTurns as guest (guest.participant_id)}
        <button
          class="pill"
          onclick={() => loadRound(guest.participant_id)}
          data-testid={`tonight-hand-to-${guest.participant_id}`}>pass to {guest.name}</button
        >
      {/each}
    </div>
  {/if}

  {#if tonight.step === 'ballot' && tonight.ballot?.slate}
    <div class="card" data-testid="tonight-ballot">
      <h2>Tap what you'd be happy with</h2>
      <p class="why">Approvals stay hidden until everyone has submitted.</p>
      <ul class="slate">
        {#each tonight.ballot.slate as card (card.title_id)}
          <li>
            <button
              class="pill"
              aria-pressed={tonight.approved.includes(card.title_id)}
              onclick={() => toggleApproval(card.title_id)}
              data-testid={`tonight-approve-${card.title_id}`}
            >
              {card.name}
              {#if card.slot === 'wildcard'}<span class="why">· wildcard</span>{/if}
            </button>
          </li>
        {/each}
      </ul>
      <button
        class="pill on"
        onclick={() => submitBallot(me?.participant_id)}
        disabled={tonight.busy || !me}
        data-testid="tonight-submit-ballot">Submit</button
      >
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
        <h2>{tonight.result.winner?.name}</h2>
        <p class="why">
          {tonight.result.winner?.year} · {tonight.result.winner?.runtime_min} min
        </p>
        <p class="data" data-testid="tonight-approval-share">{approvalShare(tonight.result)}</p>
        {#if tonight.result.unanimous}
          <p class="data" data-testid="tonight-unanimous">Unanimous.</p>
        {/if}
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
            <li class="why">{card.name} · {card.approvals} approved</li>
          {/each}
          {#if (tonight.result.runners_up ?? []).length === 0}
            <li class="why">nothing else was in the running</li>
          {/if}
        </ul>
      </div>

      {#if tonight.result.wildcard}
        <div class="wildcard card" data-testid="tonight-wildcard">
          <p class="data label">WILDCARD</p>
          <p>{tonight.result.wildcard.name}</p>
          <!-- §6.4's "honestly labelled". Served, not spelled here: the words are the rule. -->
          <p class="why">{tonight.result.wildcard.label}, honestly labelled</p>
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
            <li class="card" data-testid={`tonight-pick-${pick.title_id}`}>
              <span class="big">{pick.name}</span>
              <span class="why">{pick.why}</span>
              <span class="data">{pick.fit_line}</span>
            </li>
          {/each}
        </ul>
        {#if tonight.solo.wildcard}
          <div class="card" data-testid="tonight-solo-wildcard">
            <span class="big">{tonight.solo.wildcard.name}</span>
            <span class="why">{tonight.solo.wildcard.why}</span>
            <span class="data">{tonight.solo.wildcard.fit_line}</span>
          </div>
        {/if}
        <div class="row">
          <button
            class="pill"
            onclick={() => loadSolo({ reshuffle: true })}
            data-testid="tonight-reshuffle">Reshuffle</button
          >
          {#if tonight.solo.pair && !sharpening}
            <button
              class="pill"
              onclick={() => (sharpening = true)}
              data-testid="tonight-sharpen">sharpen this</button
            >
          {/if}
        </div>
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
                  class="poster"
                  onclick={() => sharpen(side)}
                  disabled={tonight.busy}
                  data-testid={`tonight-sharpen-${side}`}
                >
                  <span class="big">{title?.name}</span>
                  <span class="why">{title?.year} · {title?.fit_line}</span>
                </button>
              {/each}
            </div>
            <div class="row">
              {#each ANSWERS.filter((a) => a.value !== 'A' && a.value !== 'B') as choice}
                <button
                  class="pill"
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
     which is the one colour §6.8's surface does not otherwise contain. */
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
  .code { font-size: 22px; letter-spacing: 0.18em; color: var(--ember); }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .poster {
    min-height: 140px; display: flex; flex-direction: column; justify-content: flex-end;
    gap: 6px; padding: 14px; background: var(--card-raised);
    border: 1px solid var(--line-2); border-radius: var(--r-lg); color: var(--ink);
    text-align: left; cursor: pointer;
  }
  .poster:hover, .poster:focus-visible { border-color: var(--ember-edge); }
  .beat { letter-spacing: 0.2em; color: var(--ember); }
  .winner { border-color: var(--ember-edge); }
  /* `.slate` rows are plain list items; `.picks` rows are cards, so they take the card's own
     padding rather than a bare vertical rhythm. Sharing one rule left them with no horizontal
     padding at all, text starting on the border. */
  .slate li { padding: 6px 0; }
  /* The pick cards and the wildcard beside them are the same object and lay out the same way.
     Only the list items had the column rule, so the wildcard's three spans ran together on one
     line: "Tampopo a stretch - outside your usual fits your 130 min". */
  .picks li, .solo > .card { display: flex; flex-direction: column; gap: 4px; }
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
  .empty { color: var(--ink-2); font-size: 13px; }
  .rail { list-style: none; margin: 8px 0 0; padding: 8px 0 0; border-top: 1px solid var(--line); }
  .disabled { opacity: 0.55; }
  /* §6 preamble's 48 px floor. design.css raises `button.pill` on a coarse pointer; the Play
     CTA is an <a> (§7.1's deep link) and an aria-disabled <span>, so neither is reached by it. */
  .play {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: var(--touch); padding-inline: 20px;
  }
  @media (max-width: 560px) {
    .doors, .pair { grid-template-columns: 1fr; }
  }
</style>
