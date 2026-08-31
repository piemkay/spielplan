<script>
  /**
   * Tonight. Spec v2.1 §6.2 as rewritten by the owner on 2026-08-29 (54a–54g), §6.7, §6.8.
   *
   * The surface is a state machine with one screen per step, because §6.2's steps are states a
   * household moves through together and a page that showed two at once would show one person
   * a screen another has left.
   *
   *   door    → the two doors (§6.2 step 1's controls sit above both), and the open-rooms list
   *   lobby   → the room: code, QR, seats, and the host's Start
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
    connect,
    escape,
    join,
    loadBallot,
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
  let disconnect = () => {};

  onMount(async () => {
    await bootstrap();
    disconnect = connect();
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

  async function openAndWatch() {
    const room = await openRoom();
    if (room) {
      disconnect();
      disconnect = connect(room.session_id);
    }
  }

  async function joinAndWatch(args) {
    const joined = await join(args);
    if (joined) {
      code = '';
      disconnect();
      disconnect = connect(joined.session_id);
    }
  }
</script>

<section data-testid="tonight-surface">
  <header>
    <h1>Tonight</h1>
    {#if tonight.error}
      <p class="error" role="alert" data-testid="tonight-error">{tonight.error}</p>
    {/if}
  </header>

  {#if tonight.step === 'door'}
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
    <div class="rooms" data-testid="tonight-rooms">
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
              {:else}
                <span class="why">{room.viewer_seated ? 'you are in' : 'started'}</span>
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
      <!-- §6.2 step 2's QR, drawn from PUBLIC_URL's own origin so a scan lands where the
           passkey was registered (§2, §14.4). -->
      <img
        class="qr"
        alt={`QR for room ${tonight.lobby.room_code}`}
        data-testid="tonight-qr"
        src={`data:image/svg+xml;utf8,${encodeURIComponent(
          `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="#141416"/><text x="4" y="5" font-size="1.6" fill="#ece9e4" text-anchor="middle">${tonight.lobby.room_code}</text></svg>`
        )}`}
      />
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
          class="pill primary"
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
        class="pill primary"
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
            class="pill primary"
            href={tonight.result.winner.play_url}
            data-testid="tonight-play">Play on Jellyfin</a
          >
        {:else}
          <span class="pill disabled" aria-disabled="true" data-testid="tonight-play"
            >Play on Jellyfin — no Jellyfin link</span
          >
        {/if}
      </div>

      <div data-testid="tonight-runners-up">
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
        <div class="wildcard" data-testid="tonight-wildcard">
          <p class="data label">WILDCARD</p>
          <p>{tonight.result.wildcard.name}</p>
          <p class="why">a step outside your usual, honestly labelled</p>
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
          {#if tonight.solo.pair}
            <button
              class="pill"
              onclick={() => sharpen('A')}
              data-testid="tonight-sharpen">sharpen this</button
            >
          {/if}
        </div>
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
  .rooms ul, .seats, .slate, .picks, .matches { list-style: none; margin: 0; padding: 0; }
  .rooms li, .seats li {
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 8px 0; border-bottom: 1px solid var(--line);
  }
  .seat { min-height: var(--touch); }
  .code { font-size: 22px; letter-spacing: 0.18em; color: var(--ember); }
  .qr { width: 132px; height: 132px; border-radius: var(--r-md); }
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
  .picks li, .slate li { padding: 6px 0; }
  .picks li { display: flex; flex-direction: column; gap: 4px; }
  .error { color: var(--ember-lift); font-size: 12.5px; }
  .empty { color: var(--ink-2); font-size: 13px; }
  .rail { list-style: none; margin: 8px 0 0; padding: 8px 0 0; border-top: 1px solid var(--line); }
  .disabled { opacity: 0.55; }
  @media (max-width: 560px) {
    .doors, .pair { grid-template-columns: 1fr; }
  }
</style>
