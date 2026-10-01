<script>
  // One screen per step: door, solo, and a room's lobby, round, waiting, ballot and reveal. The pool
  // is never sent here, so nothing can draw it even by accident (§6.2 step 3).
  import { onDestroy, onMount } from 'svelte';
  import { replaceState } from '$app/navigation';
  import ActionSheet from '$lib/components/ActionSheet.svelte';
  import RatePeek from '$lib/components/RatePeek.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import Avatar from '$lib/components/Avatar.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import { session } from '$lib/session.svelte.js';
  import { topbar } from '$lib/topbar.svelte.js';
  import {
    ANSWERS,
    BUDGET_MAX,
    BUDGET_MIN,
    BUDGET_STEP,
    ESCAPE_LABEL,
    ESCAPE_LOCKED_LINE,
    FIRST_PAIR_LINE,
    GUEST_FIRST_PAIR_LINE,
    JOIN_CAPTION,
    LOBBY_LINE,
    MAX_GUESTS,
    MAX_VETOES,
    REVEAL_BEAT,
    ROUND_QUESTION,
    SHARE_CAPTION,
    SOLO_DOOR_LINE,
    SOLO_ESCAPE_LABEL,
    WILDCARD_LINE,
    WRAPPED_LINE,
    answer,
    answerSolo,
    approvalShare,
    ballotTurns,
    ballotWaitingLine,
    bootstrap,
    breadthLine,
    budgetLabel,
    budgetSoftLine,
    chooseKind,
    leave,
    connect,
    endRoom,
    escape,
    escapeSolo,
    followLink,
    handBallot,
    join,
    linkedRoom,
    loadRooms,
    loadRound,
    loadSolo,
    myVetoKeys,
    onlyYesLines,
    openRoom,
    othersVetoLines,
    pairFacts,
    pickLabel,
    progressLines,
    rememberBudget,
    restoreBudget,
    roomEvening,
    roomLine,
    roomVetoLine,
    roundDots,
    roundHeader,
    settingsDetail,
    settingsTitle,
    shareRoom,
    start,
    stopClock,
    submitBallot,
    submitLabel,
    toggleApproval,
    toggleVeto,
    tonight,
    undo,
    undoSolo,
    vetoCaption,
    waitingLine
  } from '$lib/tonight.svelte.js';
  // The one runtime formatter, so a series reads `45m/ep` here too.
  import { metaLine, runtimeLabel, sentenceCase } from '$lib/rate.svelte.js';
  import { playWhy } from '$lib/titleCard.js';
  import { preloadPoster, ready } from '$lib/art.js';
  import { haptic } from '$lib/motion.js';

  let code = $state('');
  let ending = $state(false);
  let settingsOpen = $state(false);
  let opening = $state('');
  // The answer in flight and the pair it answers: the posters take it before the reply lands.
  let sent = $state(null);
  // The film a poster tap is looking at; a look never answers.
  let peek = $state(null);
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
  // Solo's round, before its picks (decision 532).
  const soloAsking = $derived(tonight.step === 'solo' && !!tonight.solo?.pair);
  // A room, and solo's round, is a full-screen flow over the tab bar (§6 preamble, decision 527).
  const inFlow = $derived(
    tonight.booted &&
      (['lobby', 'round', 'waiting', 'ballot', 'reveal'].includes(tonight.step) || soloAsking)
  );
  // The door names the place in the shell's top row (decision 528); a room brings its own bar.
  $effect(() => {
    if (!topbar.host || tonight.step !== 'door') return;
    topbar.content = doorBar;
    return () => {
      if (topbar.content === doorBar) topbar.content = null;
    };
  });
  // Host-only, and on every step a room can stall in.
  const canEnd = $derived(isHost && tonight.step !== 'reveal');
  // Guests answer on the host's phone once every earlier seat has finished (§6.2 step 2).
  const guestTurns = $derived(
    isHost
      ? (tonight.lobby?.seats ?? []).filter((s) => s.role === 'guest' && !s.ended_by)
      : []
  );
  // A guest's first pair says they need not have seen the films; a member's room seat says nothing.
  const playingGuest = $derived(
    (tonight.lobby?.seats ?? []).some(
      (s) => s.participant_id === tonight.activeSeat && s.role === 'guest'
    )
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
  const seated = $derived(tonight.lobby?.seats?.length ?? 0);
  const perEpisode = $derived(tonight.controls.kind === 'series' ? ' per episode' : '');
  const budgetFill = $derived(
    ((tonight.controls.runtime_budget_min - BUDGET_MIN) / (BUDGET_MAX - BUDGET_MIN)) * 100
  );

  // Back to the door; the seat is kept, and the rooms list offers resume.
  async function toDoor() {
    leave();
    // Stop watching the room too, or a session frame would re-read it.
    watch(null);
    ending = false;
    await loadRooms();
  }

  // Ending is terminal and releases the room code, so the action sheet asks first.
  async function endTheRoom() {
    await endRoom();
    ending = false;
    watch(null);
  }

  // Remember the budget when an evening is opened, not on every nudge.
  function rememberControls() {
    rememberBudget(session.user?.id, tonight.controls.kind, tonight.controls.runtime_budget_min);
  }

  async function openAndWatch() {
    opening = 'room';
    const room = await openRoom();
    opening = '';
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

  function setGuests(n) {
    tonight.controls.guests = Math.max(0, Math.min(MAX_GUESTS, n));
  }

  function seatLine(seat) {
    if (seat.role === 'guest') {
      return isHost ? 'On this phone' : `On ${tonight.lobby?.host?.name}'s phone`;
    }
    const you = seat.user_id === session.user?.id;
    const host = seat.user_id === tonight.lobby?.host?.user_id;
    if (you) return host ? 'You · host' : 'You';
    return host ? 'Host' : 'Joined';
  }

  // The account behind a seat, which its avatar's colour is keyed on; a guest has none.
  function personOf(participantId) {
    const seat = (tonight.lobby?.seats ?? []).find((s) => s.participant_id === participantId);
    return seat?.user_id == null
      ? null
      : { id: seat.user_id, role: seat.account_role, colour: seat.colour };
  }

  // Everyone but the seat on this screen.
  const others = $derived(progressLines(tonight.progress, tonight.activeSeat));

  async function say(key, onAnswer, value) {
    sent = { key, value };
    await onAnswer(value);
    sent = null;
  }
  const pose = (said, side) => (!said ? '' : said === side || said === 'EITHER' ? 'up' : 'down');

  // The reveal plays only when this page saw the last vote land, never on a reload or a re-read,
  // and lights once the result and the winner's poster are in (decision 530).
  let playing = $state(false);
  let lit = $state(false);
  let lastStep = '';
  $effect(() => {
    if (tonight.step === 'reveal' && lastStep !== 'reveal') {
      playing = lastStep === 'waiting' || lastStep === 'ballot';
      lit = false;
    }
    lastStep = tonight.step;
  });
  $effect(() => {
    const result = tonight.result;
    if (!playing || !result || lit) return;
    let live = true;
    const light = () => live && (lit = true);
    const wait = ready([preloadPoster(result.winner)], 700);
    if (wait) wait.then(light);
    else light();
    return () => (live = false);
  });

  const low = (s) => (s ? s.charAt(0).toLowerCase() + s.slice(1) : '');
  // Green only when the title fits; over is a plain fact, not a warning.
  const fits = (t) => t?.runtime_min != null && !t?.over_budget_min;
</script>

{#snippet problem()}
  {#if tonight.error}
    <p class="error" role="alert" data-testid="tonight-error">{tonight.error}</p>
  {/if}
{/snippet}

{#snippet doorBar()}
  <h1 class="bar-title">Tonight</h1>
{/snippet}

{#snippet working(busy)}
  {#if busy}<span class="spinner"></span>{:else}<Icon name="chevron-right" size={16} />{/if}
{/snippet}

{#snippet bar(title)}
  <header class="bar">
    <!-- Back keeps the seat, so a restored room is never a trap. -->
    <button class="btn-plain back" onclick={toDoor} data-testid="tonight-back">
      <Icon name="chevron-left" />Tonight
    </button>
    <h1 class="bar-title">{title}</h1>
    <span class="bar-end">
      {#if canEnd}
        <button
          class="btn-plain btn-destructive"
          onclick={() => (ending = true)}
          data-testid="tonight-end-room">End</button
        >
      {/if}
    </span>
  </header>
{/snippet}

{#snippet play(pick, big)}
  {#if pick.play_url}
    <a
      class={big ? 'btn-primary play' : 'play-icon'}
      href={pick.play_url}
      aria-label="Play {pick.name} on Jellyfin"
      data-testid="tonight-play-{pick.title_id}"
    >
      <Icon name="play" size={big ? 20 : 24} />{#if big}<span>Play</span>{/if}
    </a>
  {:else if big}
    <button class="btn-primary play" disabled aria-describedby="play-why-{pick.title_id}"
      ><Icon name="play" size={20} /><span>Play</span></button
    >
    <span class="footnote" id="play-why-{pick.title_id}">{playWhy(pick.play_reason ?? 'no_server')}</span>
  {:else}
    <button
      class="play-icon"
      disabled
      aria-label="Play {pick.name} on Jellyfin. {playWhy(pick.play_reason ?? 'no_server')}"
    >
      <Icon name="play" />
    </button>
  {/if}
{/snippet}

<!-- Where each of the others has got to, never an answer (54c). -->
{#snippet whereOthersAre(testid)}
  <div class="blind">
    <ul class="others" data-testid={testid}>
      {#each others as other (other.participant_id)}
        <li>
          <Avatar name={other.name} person={personOf(other.participant_id)} size={24} />
          <span>{other.line}</span>
        </li>
      {/each}
    </ul>
    <p class="footnote">Answers stay hidden until everyone's done.</p>
  </div>
{/snippet}

<!-- A round's bar and dots: a room's seat or solo's (decision 532). -->
{#snippet roundTop(state, onUndo, canUndo, ids)}
  {@const now = (state.answered ?? 0) + 1}
  <header class="bar">
    <button class="btn-plain back" onclick={toDoor} data-testid="tonight-back">Leave</button>
    <!-- What to expect, not the cap, which the round is built to avoid. -->
    <p class="bar-count figures" data-testid={ids.count}>{roundHeader(state)}</p>
    <span class="bar-end">
      <!-- Always there, dimmed with nothing to take back. -->
      <button class="btn-plain" onclick={onUndo} disabled={!canUndo} data-testid={ids.undo}>Undo</button>
    </span>
  </header>
  <div class="dots" aria-hidden="true">
    {#each { length: roundDots(state) }, i (i)}
      <span class:on={i < now}></span>
    {/each}
  </div>
{/snippet}

{#snippet escapeControl(available, onEscape, label, testid)}
  {#if available}
    <button class="btn-plain" onclick={onEscape} data-testid={testid}>{label}</button>
  {:else}
    <p class="locked" data-testid="{testid}-locked">
      <span>{label}</span>
      <span class="footnote">{ESCAPE_LOCKED_LINE}</span>
    </p>
  {/if}
{/snippet}

<!-- The round's question (§6.2 step 4): two films, "This one" under each, and the two level answers. -->
{#snippet chooser(pair, onAnswer, ids, firstLine)}
  {@const key = `${pair.a?.title_id}:${pair.b?.title_id}`}
  {@const said = sent?.key === key ? sent.value : null}
  <h2 class="title-1 question">{ROUND_QUESTION}</h2>
  <!-- Keyed on both titles, so the next pair deals in and a re-read of this one replays nothing. -->
  {#key key}
    <div class="pair">
      {#each [['A', pair.a], ['B', pair.b]] as [side, title] (side)}
        <div class="choice {pose(said, side)}">
          <!-- A poster opens About and never answers. `.art`, never `.poster`: design.css's
               `.poster` is a 2:3 frame that fills the phone. -->
          <button
            class="art"
            aria-label="About {title?.name}"
            aria-haspopup="dialog"
            onclick={() => (peek = posterOf(title))}
            data-testid="{ids.pick}-about-{side}"
          >
            <RatePoster title={posterOf(title)} showName={false} />
            <span class="info" aria-hidden="true">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="1.75" stroke-linecap="round"><circle cx="12" cy="7.5" r="1"
                  fill="currentColor" stroke="none" /><path d="M12 10.75v5.75" /></svg>
            </span>
          </button>
          <span class="choice-name">{title?.name}</span>
          {#each pairFacts(title) as fact, i (i)}
            <span class="fact" data-testid={ids.fact && `${ids.fact}-${side}`}>{fact}</span>
          {/each}
        </div>
      {/each}
    </div>
  {/key}
  <div class="answers">
    {#each ANSWERS as choice (choice.value)}
      {@const title = choice.value === 'A' ? pair.a : choice.value === 'B' ? pair.b : null}
      <button
        class="btn-secondary"
        class:held={said === choice.value}
        aria-label={title ? `${title.name}: this one` : null}
        onclick={() => say(key, onAnswer, choice.value)}
        disabled={tonight.busy}
        data-testid={title ? `${ids.pick}-${choice.value}` : `${ids.answer}-${choice.value}`}
        >{choice.label}</button
      >
    {/each}
  </div>
  {#if firstLine}
    <p class="why center" data-testid="tonight-first-pair">{firstLine}</p>
  {/if}
{/snippet}

<section class="tonight" class:flow={inFlow} data-testid="tonight-surface">
  {#if !tonight.booted}
    <!-- Until the restore lands, a live door could open a second room for someone already seated. -->
    <div class="screen at-door" data-testid="tonight-booting">
      <p class="sr-only" role="status">Loading…</p>
      <div class="doors">
        <span class="skeleton door-shape"></span>
        <span class="skeleton door-shape"></span>
      </div>
    </div>
  {:else if tonight.step === 'door'}
    <div class="screen at-door">
      {#if !topbar.host}{@render doorBar()}{/if}
      {@render problem()}
      {#if tonight.notice}<p class="why" role="alert" data-testid="tonight-notice">{tonight.notice}</p>{/if}
      <div class="fork">
        <div class="doors">
          <button class="door press" onclick={openAndWatch} disabled={tonight.busy} data-testid="tonight-open">
            <span class="door-top">
              <span class="tile blue"><Icon name="people" size={18} /></span>
              <span class="chev">{@render working(opening === 'room')}</span>
            </span>
            <span class="door-name">Watch together</span>
            <span class="why"
              >{opening === 'room'
                ? 'Opening a room…'
                : `Everyone answers a few quick pairs on their own phone, then we reveal one ${tonight
                    .controls.kind === 'series'
                    ? 'series'
                    : 'film'} you'll all enjoy.`}</span
            >
          </button>
          <button
            class="door press"
            onclick={async () => {
              rememberControls();
              opening = 'solo';
              await loadSolo();
              opening = '';
            }}
            disabled={tonight.busy}
            data-testid="tonight-solo-door"
          >
            <span class="door-top">
              <span class="tile ember"><Icon name="person" size={18} /></span>
              <span class="chev">{@render working(opening === 'solo')}</span>
            </span>
            <span class="door-name">Just me</span>
            <span class="why">{opening === 'solo' ? 'Finding your first pair…' : SOLO_DOOR_LINE}</span>
          </button>
        </div>

        <!-- §6.2 step 1: the controls sit before the fork and apply to both, behind one row. -->
        <div class="list-group" data-testid="tonight-controls">
          <button class="list-row summary" onclick={() => (settingsOpen = true)} data-testid="tonight-settings">
            <span class="tile graphite"><Icon name="sliders" size={18} /></span>
            <span class="row-text">
              <span class="figures" data-testid="tonight-summary">{settingsTitle(tonight.controls)}</span>
              <span class="footnote">{settingsDetail(tonight.controls)}</span>
            </span>
            <span class="change">Change</span>
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </button>
        </div>
      </div>

      <section class="group">
        <h2 class="list-header">Join a room</h2>
        <form
          class="join"
          onsubmit={(e) => {
            e.preventDefault();
            joinAndWatch({ roomCode: code });
          }}
        >
          <input
            bind:value={code}
            placeholder="Room code, e.g. MX-2210"
            aria-label="Room code"
            autocomplete="off"
            autocapitalize="characters"
            data-testid="tonight-code"
          />
          <button class="capsule hit" type="submit" data-testid="tonight-join">Join</button>
        </form>
      </section>

      <section class="group" data-testid="tonight-rooms">
        <h2 class="list-header">Open rooms</h2>
        {#if tonight.rooms.length === 0}
          <p class="list-footer" data-testid="tonight-no-rooms">No room is open right now.</p>
        {:else}
          <ul class="list-group rows">
            {#each tonight.rooms as room (room.session_id)}
              <li class="room" data-testid={`tonight-room-${room.room_code}`}>
                <Avatar name={room.host} person={room.host_avatar} size={36} />
                <span class="row-text">
                  <span>{room.host}'s room</span>
                  <span class="footnote figures">{roomLine(room)}</span>
                  <span class="footnote">{roomEvening(room)}</span>
                  {#if roomVetoLine(room)}<span class="footnote">{roomVetoLine(room)}</span>{/if}
                </span>
                {#if room.joinable}
                  <button
                    class="capsule tinted hit"
                    onclick={() => joinAndWatch({ sessionId: room.session_id })}
                    aria-label="Join {room.host}'s room"
                    data-testid={`tonight-seat-${room.room_code}`}>Join</button
                  >
                {:else if room.viewer_seated}
                  <button
                    class="capsule hit"
                    onclick={() => joinAndWatch({ sessionId: room.session_id })}
                    data-testid={`tonight-resume-${room.room_code}`}>Resume</button
                  >
                {:else}
                  <span class="footnote">Started</span>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
        <p class="list-footer">{JOIN_CAPTION}</p>
      </section>
    </div>
  {:else if soloAsking}
    <div class="screen round" data-testid="tonight-mood">
      {@render roundTop(tonight.solo, undoSolo, tonight.soloAnswers.length > 0, {
        count: 'tonight-mood-count',
        undo: 'tonight-mood-undo'
      })}
      {@render problem()}
      {@render chooser(
        tonight.solo.pair,
        answerSolo,
        { pick: 'tonight-mood', answer: 'tonight-mood', fact: 'tonight-mood-fact' },
        tonight.solo.answered ? null : FIRST_PAIR_LINE
      )}
      <div class="escape">
        {@render escapeControl(tonight.solo.escape_available, escapeSolo, SOLO_ESCAPE_LABEL, 'tonight-mood-escape')}
      </div>
    </div>
  {:else if tonight.step === 'solo' && tonight.solo}
    {@const [hero, ...rest] = tonight.solo.picks ?? []}
    {@const wildcard = tonight.solo.wildcard}
    <div class="screen solo" data-testid="tonight-solo">
      <button class="btn-plain back" onclick={toDoor} data-testid="tonight-back">
        <Icon name="chevron-left" />Tonight
      </button>
      <div class="solo-head">
        <h1 class="large-title">Tonight, for {session.user?.name ?? 'you'}</h1>
        <p class="footnote figures" data-testid="tonight-provenance">{tonight.solo.provenance}</p>
      </div>
      {#if tonight.solo.no_round}
        <!-- Too few films on the ladder to ask about: straight to the picks, saying why. -->
        <p class="why" data-testid="tonight-no-round">{tonight.solo.no_round}</p>
      {/if}
      {@render problem()}
      {#if tonight.solo.empty}
        <p class="why" data-testid="tonight-solo-empty">{tonight.solo.empty}</p>
      {:else}
        <!-- A new set of picks rises in; a walk that returns the same set only reorders it. -->
        {#key (tonight.solo.picks ?? []).map((p) => p.title_id).sort().join()}
          <div class="picks" data-testid="tonight-picks">
            {#if hero}
              <article class="hero rise" data-testid={`tonight-pick-${hero.title_id}`}>
                <span class="hero-art"><RatePoster title={posterOf(hero)} showName={false} /></span>
                <div class="hero-text">
                  <div class="stack">
                    <h2 class="title-1">{hero.name}</h2>
                    <p class="why figures">{metaLine(hero)}</p>
                    <p class="why" data-testid="tonight-why">{hero.why}</p>
                  </div>
                  <div class="hero-actions">
                    <span class="badge" class:ok={fits(hero)} data-testid="tonight-fit"
                      ><span class="dot"></span>{hero.fit_line}</span
                    >
                    {@render play(hero, true)}
                  </div>
                </div>
              </article>
            {/if}
            <ul class="list-group rows">
              {#each rest as pick, i (pick.title_id)}
                <li class="pick rise" style:--i={i + 1} data-testid={`tonight-pick-${pick.title_id}`}>
                  <span class="thumb"><RatePoster title={posterOf(pick)} showName={false} /></span>
                  <span class="row-text">
                    <span class="pick-name">{pick.name}</span>
                    <span class="why" data-testid="tonight-why">{pick.why}</span>
                    <span class="footnote figures" data-testid="tonight-fit"
                      >{[runtimeLabel(pick), low(pick.fit_line)].filter(Boolean).join(' · ')}</span
                    >
                  </span>
                  {@render play(pick, false)}
                </li>
              {/each}
              {#if wildcard}
                <li class="pick rise" style:--i={rest.length + 1} data-testid="tonight-solo-wildcard">
                  <span class="thumb"><RatePoster title={posterOf(wildcard)} showName={false} /></span>
                  <span class="row-text">
                    <span class="label"><Icon name="sparkle" size={14} />Wildcard</span>
                    <span class="pick-name">{wildcard.name}</span>
                    <span class="why" data-testid="tonight-why">{wildcard.why}</span>
                    <span class="footnote figures" data-testid="tonight-fit"
                      >{[runtimeLabel(wildcard), low(wildcard.fit_line)].filter(Boolean).join(' · ')}</span
                    >
                  </span>
                  {@render play(wildcard, false)}
                </li>
              {/if}
            </ul>
          </div>
        {/key}
        <button
          class="btn-secondary"
          onclick={() => loadSolo({ reshuffle: true })}
          disabled={tonight.busy}
          data-testid="tonight-reshuffle"><Icon name="dice" size={20} />Reshuffle</button
        >
        {#if tonight.solo.wrapped}
          <!-- A reshuffle that has come back round returns titles already seen here, so say so. -->
          <p class="footnote" data-testid="tonight-wrapped">{WRAPPED_LINE}</p>
        {/if}
      {/if}
    </div>
  {:else if tonight.step === 'lobby' && tonight.lobby}
    <div class="screen" data-testid="tonight-lobby">
      {@render bar(isHost ? 'Your room' : `${tonight.lobby.host?.name}'s room`)}
      {@render problem()}
      <section class="code-card">
        <h2 class="list-header">Room code</h2>
        <p class="room-code" data-testid="tonight-room-code">{tonight.lobby.room_code}</p>
        <p class="footnote" data-testid="tonight-share-caption">{SHARE_CAPTION}</p>
        <!-- Share opens the phone's sheet; without one the link is written out below. -->
        <button class="capsule hit" onclick={shareRoom} data-testid="tonight-share">
          <Icon name="share" size={18} />Share link
        </button>
        {#if tonight.shareUrl}
          <p class="footnote link" data-testid="tonight-share-url">{tonight.shareUrl}</p>
        {/if}
      </section>

      <section class="group">
        <h2 class="list-header">Who's in</h2>
        <ul class="list-group rows" data-testid="tonight-seats">
          {#each tonight.lobby.seats as seat (seat.participant_id)}
            <li class="seat">
              <Avatar name={seat.name} person={personOf(seat.participant_id)} />
              <span class="row-text">
                <span>{seat.name}</span>
                <span class="footnote">{seatLine(seat)}</span>
              </span>
              <span class="badge ok"><span class="dot"></span>Ready</span>
            </li>
          {/each}
        </ul>
      </section>

      <!-- Up to three chips each; the pool leaves out anything anyone ruled out (decision 505). -->
      <section class="group" data-testid="tonight-vetoes">
        <h2 class="list-header">Not tonight</h2>
        <div class="chips">
          {#each tonight.lobby.veto_options ?? [] as option (option.key)}
            {@const on = vetoKeys.includes(option.key)}
            <button
              class="pill"
              aria-pressed={on}
              disabled={tonight.busy || (!on && vetoKeys.length >= MAX_VETOES)}
              onclick={() => toggleVeto(option.key, session.user?.id)}
              data-testid={`tonight-veto-${option.key}`}>{sentenceCase(option.label)}</button
            >
          {/each}
        </div>
        {#each othersVetoes as line (line)}
          <p class="list-footer" data-testid="tonight-others-vetoes">{line}</p>
        {/each}
        <p class="list-footer" data-testid="tonight-veto-caption">{vetoCaption(tonight.lobby.kind)}</p>
      </section>

      <section class="group">
        <div class="list-group">
          <div class="list-row summary">
            <span class="tile graphite"><Icon name="sliders" size={18} /></span>
            <span class="row-text">
              <span class="figures">{settingsTitle(tonight.lobby)}</span>
              <span class="footnote">{settingsDetail({ include_rewatches: tonight.lobby.include_rewatches })}</span>
            </span>
          </div>
        </div>
        <p class="list-footer" data-testid="tonight-mood-caption">{LOBBY_LINE}</p>
      </section>

      <div class="dock">
        {#if isHost}
          <button class="btn-primary wide" onclick={start} disabled={tonight.busy} data-testid="tonight-start"
            >Start — {seated} {seated === 1 ? 'person' : 'people'}</button
          >
        {:else}
          <p class="footnote center" data-testid="tonight-waiting-for-host">
            {tonight.lobby.host?.name} starts when everyone is in.
          </p>
        {/if}
      </div>
    </div>
  {:else if tonight.step === 'round' && tonight.round?.pair}
    <div class="screen round" data-testid="tonight-round">
      {@render roundTop(tonight.round, undo, tonight.round.answered > 0, {
        count: 'tonight-round-count',
        undo: 'tonight-undo'
      })}
      {@render problem()}
      {@render chooser(
        tonight.round.pair,
        answer,
        { pick: 'tonight-pick', answer: 'tonight-answer', fact: 'tonight-pair-fact' },
        playingGuest && !tonight.round.answered ? GUEST_FIRST_PAIR_LINE : null
      )}
      {#if others.length}
        {@render whereOthersAre('tonight-round-progress')}
      {/if}
      <div class="escape">
        {@render escapeControl(tonight.round.escape_available, escape, ESCAPE_LABEL, 'tonight-escape')}
        {#if canEnd}
          <button class="btn-plain btn-destructive" onclick={() => (ending = true)} data-testid="tonight-end-room"
            >End the room</button
          >
        {/if}
      </div>
    </div>
  {:else if tonight.step === 'waiting'}
    <div class="screen" data-testid="tonight-waiting">
      {@render bar(tonight.lobby?.state === 'ballot' ? 'Votes' : 'Waiting')}
      {@render problem()}
      {#if tonight.lobby?.state === 'ballot'}
        <!-- After this phone has voted, the ballot's status, not the round's counts. -->
        <h2 class="title-1">Waiting for the others</h2>
        <p class="why" data-testid="tonight-ballot-waiting">{ballotWaitingLine(tonight.ballot)}</p>
        <p class="footnote">Nobody sees anybody's votes until every vote is in.</p>
      {:else}
        {#if tonight.round?.no_round}
          <h2 class="title-1">Waiting for the others</h2>
          <p class="why" data-testid="tonight-no-round">{tonight.round.no_round}</p>
        {:else}
          <h2 class="title-1">Your answers are in</h2>
        {/if}
        {#if waitingLine(tonight.progress)}
          <p class="why" data-testid="tonight-waiting-for">{waitingLine(tonight.progress)}</p>
        {/if}
        {@render whereOthersAre('tonight-progress')}
        {#if guestTurns.length}
          <!-- Guests take turns in seat order, so there is one next step (§6.2 step 2). -->
          {@const [next, ...later] = guestTurns}
          <button
            class="btn-primary wide"
            onclick={() => loadRound(next.participant_id)}
            data-testid={`tonight-hand-to-${next.participant_id}`}>Pass the phone to {next.name}</button
          >
          {#if later.length}
            <p class="footnote center" data-testid="tonight-later-turns">
              Then: {later.map((g) => g.name).join(', ')}
            </p>
          {/if}
        {/if}
      {/if}
    </div>
  {:else if tonight.step === 'ballot' && tonight.ballot?.slate}
    <div class="screen" data-testid="tonight-ballot">
      {@render bar('Vote')}
      {@render problem()}
      <div class="stack">
        <h2 class="title-1">Which would you be happy with?</h2>
        <p class="footnote">Tap every one you'd watch. Votes stay hidden until everyone has voted.</p>
      </div>
      {#if ballotOpen}
        <!-- Whose ballot this is: on the host's phone it is not always the owner's. -->
        <p class="whose"><span data-testid="tonight-ballot-seat">{ballotSeat.name}</span>'s turn</p>
        <ul class="list-group rows">
          {#each tonight.ballot.slate as card (card.title_id)}
            {@const picked = tonight.approved.includes(card.title_id)}
            <li>
              <button
                class="option"
                aria-pressed={picked}
                onclick={() => {
                  haptic();
                  toggleApproval(card.title_id);
                }}
                data-testid={`tonight-approve-${card.title_id}`}
              >
                <span class="thumb"><RatePoster title={posterOf(card)} showName={false} /></span>
                <span class="row-text">
                  {#if card.slot === 'wildcard'}
                    <span class="label"><Icon name="sparkle" size={14} />Wildcard</span>
                  {/if}
                  <span class="pick-name">{card.name}</span>
                  <span class="footnote figures">{metaLine({ ...card, kind: tonight.lobby?.kind })}</span>
                  {#if card.slot === 'wildcard'}<span class="why">{WILDCARD_LINE}</span>{/if}
                </span>
                <span class="tick" aria-hidden="true">{#if picked}<Icon name="check" size={16} />{/if}</span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
      <!-- The ballot's hand-off: without it a guest's vote could never be cast. -->
      {#each ballotSeats as guest (guest.participant_id)}
        <button
          class="btn-secondary wide"
          onclick={() => handBallot(guest.participant_id)}
          data-testid={`tonight-ballot-to-${guest.participant_id}`}>Pass to {guest.name}</button
        >
      {/each}
      <p class="footnote figures" data-testid="tonight-ballot-progress">
        {tonight.ballot.submitted} of {tonight.ballot.seated} have voted
      </p>
      {#if ballotOpen}
        <!-- Docked at the bottom, so no ballot row shows, or is tappable, beneath Submit. -->
        <div class="dock" data-testid="tonight-submit-bar">
          <button
            class="btn-primary wide"
            onclick={() => submitBallot(tonight.activeSeat)}
            disabled={tonight.busy || tonight.activeSeat === null}
            data-testid="tonight-submit-ballot">{submitLabel(tonight.approved.length)}</button
          >
        </div>
      {/if}
    </div>
  {:else if tonight.step === 'reveal'}
    {@const result = tonight.result}
    {@const winner = result?.winner}
    <!-- The stage stands before the result lands, so the last voter never sees an empty flow. -->
    <div class="screen reveal" class:playing>
      <header class="bar">
        <span></span>
        <p class="list-header beat" data-testid="tonight-beat">{REVEAL_BEAT}</p>
        <span class="bar-end">
          <button class="btn-plain done" onclick={toDoor} data-testid="tonight-back">Done</button>
        </span>
      </header>
      {@render problem()}
      {#if result && (lit || !playing)}
        <div class="result" data-testid="tonight-reveal">
          {#if winner}
            <div class="winner" data-testid="tonight-winner">
              <span class="winner-art"><RatePoster title={posterOf(winner)} showName={false} /></span>
              <h2 class="title-1">{winner.name}</h2>
              <p class="winner-meta">
                <span class="why figures">{metaLine(winner)}</span>
                <span class="badge" class:ok={fits(winner)} data-testid="tonight-fit-line"
                  ><span class="dot"></span>{winner.fit_line}</span
                >
              </p>
              {#if winner.label}
                <!-- The wildcard won: this card carries its label. -->
                <p class="label" data-testid="tonight-winner-label"><Icon name="sparkle" size={14} />{winner.label}</p>
              {/if}
              {#if winner.reserved_for}
                <p class="label" data-testid="tonight-reserved-for">{pickLabel(winner.reserved_for.name)}</p>
              {/if}
              <p class="approval">
                <span class="faces">
                  {#each result.breadth ?? [] as b (b.participant_id)}
                    <Avatar name={b.name} person={personOf(b.participant_id)} size={32} yes={b.said_yes} />
                  {/each}
                </span>
                <span data-testid="tonight-approval-share">{approvalShare(result)}</span>
              </p>
            </div>
            {#if winner.conflict}
              <p class="why lines" data-testid="tonight-conflict">
                {winner.conflict.headline}
                {winner.conflict.explanation}
              </p>
            {/if}
            <ul class="list-group rows lines" data-testid="tonight-match-lines">
              {#each winner.match_lines ?? [] as line, i (i)}
                <li class="seat">
                  <Avatar name={line.name} person={personOf(line.participant_id)} />
                  <span class="row-text">
                    <span class="pick-name">{line.name}</span>
                    <span class="why sentence">{line.line}</span>
                  </span>
                </li>
              {/each}
            </ul>
            <div class="stack lines">
              <!-- How broad each yes was, released with the reveal (54e). -->
              <p class="footnote figures" data-testid="tonight-breadth">{breadthLine(result)}</p>
              {#each onlyYesLines(result) as only (only)}
                <p class="footnote" data-testid="tonight-only-yes">{only}</p>
              {/each}
            </div>
          {/if}

          <section class="group lines" data-testid="tonight-runners-up">
            <h3 class="list-header">Runners-up</h3>
            <ul class="list-group rows">
              {#each result.runners_up ?? [] as card (card.title_id)}
                <!-- A `const` keeps the line one text node; the labels follow the card wherever it lands. -->
                {@const pick = card.reserved_for ? ` · ${pickLabel(card.reserved_for.name)}` : ''}
                <li class="pick">
                  <span class="thumb small"><RatePoster title={posterOf(card)} showName={false} /></span>
                  <span class="row-text">
                    <span>{card.name}</span>
                    <span class="footnote figures" data-testid={`tonight-runner-up-${card.title_id}`}
                      >{card.approvals} of {result.participants} said yes{pick}</span
                    >
                  </span>
                </li>
              {:else}
                <li class="pick"><span class="footnote">Nothing else was in the running.</span></li>
              {/each}
            </ul>
          </section>

          {#if result.wildcard}
            <section class="group lines" data-testid="tonight-wildcard">
              <h3 class="list-header">Wildcard</h3>
              <div class="list-group">
                <div class="pick">
                  <span class="thumb small"><RatePoster title={posterOf(result.wildcard)} showName={false} /></span>
                  <span class="row-text">
                    <span>{result.wildcard.name}</span>
                    <!-- The label is the honesty (§6.4); approvals are said here once. -->
                    <span class="footnote figures" data-testid="tonight-wildcard-line"
                      >{`${result.wildcard.label} · ${result.wildcard.approvals ?? 0} of ${result.participants} said yes`}</span
                    >
                  </span>
                </div>
              </div>
            </section>
          {/if}
        </div>
        {#if winner}
          <!-- Docked like the ballot's Submit, so Play is in thumb reach. -->
          <div class="dock">
            {#if winner.play_url}
              <a class="btn-primary play wide" href={winner.play_url} data-testid="tonight-play"
                ><Icon name="play" size={20} />Play on Jellyfin</a
              >
            {:else}
              <button class="btn-primary play wide" disabled aria-describedby="tonight-play-why" data-testid="tonight-play"
                ><Icon name="play" size={20} />Play on Jellyfin</button
              >
              <p class="footnote center" id="tonight-play-why">{playWhy(winner.play_reason ?? 'no_server')}</p>
            {/if}
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</section>

<!-- The bar sits in the body, not the header: the header's drag area captures the pointer. -->
<Sheet open={settingsOpen} onClose={() => (settingsOpen = false)} label="Tonight's settings" detent="medium" width={480}>
  {#snippet children(close)}
    <div class="sheet-bar">
      <span></span>
      <h2>Tonight's settings</h2>
      <button class="btn-plain done" onclick={close} data-testid="tonight-settings-done">Done</button>
    </div>
    <div class="settings">
      <div class="segmented" role="group" aria-label="Kind">
        {#each [['movie', 'Film'], ['series', 'Series']] as [value, label] (value)}
          <button
            aria-pressed={tonight.controls.kind === value}
            onclick={() => chooseKind(value, session.user?.id)}
            data-testid={`tonight-kind-${value}`}>{label}</button
          >
        {/each}
      </div>

      <section class="group">
        <div class="setting how-long">
          <label class="setting-line" for="tonight-budget">
            <span>How long?</span>
            <!-- On a series night the number bounds minutes per episode, and this is where it is set. -->
            <span class="readout figures" data-testid="tonight-budget-value"
              >{budgetLabel(tonight.controls.runtime_budget_min)}{perEpisode}</span
            >
          </label>
          <input
            id="tonight-budget"
            class="range"
            type="range"
            min={BUDGET_MIN}
            max={BUDGET_MAX}
            step={BUDGET_STEP}
            bind:value={tonight.controls.runtime_budget_min}
            aria-valuetext="{budgetLabel(tonight.controls.runtime_budget_min)}{perEpisode}"
            style:--fill="{budgetFill}%"
            data-testid="tonight-budget"
          />
        </div>
        <p class="list-footer" data-testid="tonight-budget-soft">{budgetSoftLine(tonight.controls.kind)}</p>
      </section>

      <section class="group">
        <label class="setting setting-line">
          <span id="tonight-rewatches-label">Include rewatches</span>
          <button
            class="switch"
            role="switch"
            aria-checked={tonight.controls.include_rewatches}
            aria-labelledby="tonight-rewatches-label"
            onclick={() => (tonight.controls.include_rewatches = !tonight.controls.include_rewatches)}
            data-testid="tonight-rewatches"><span class="knob"></span></button
          >
        </label>
        <p class="list-footer">Off skips what everyone here has seen.</p>
      </section>

      <section class="group">
        <div class="setting setting-line">
          <span class="grow">Guests</span>
          <span class="readout figures" data-testid="tonight-guests">{tonight.controls.guests}</span>
          <span class="stepper">
            <button
              class="hit"
              aria-label="Fewer guests"
              disabled={tonight.controls.guests <= 0}
              onclick={() => setGuests(tonight.controls.guests - 1)}
              data-testid="tonight-guests-less"><Icon name="minus" size={16} /></button
            >
            <span class="sep" aria-hidden="true"></span>
            <button
              class="hit"
              aria-label="More guests"
              disabled={tonight.controls.guests >= MAX_GUESTS}
              onclick={() => setGuests(tonight.controls.guests + 1)}
              data-testid="tonight-guests-more"><Icon name="plus" size={16} /></button
            >
          </span>
        </div>
        <p class="list-footer">Guests take their turn on this phone, after you.</p>
      </section>
    </div>
  {/snippet}
</Sheet>

{#if peek}
  <!-- About, without its Not seen: Tonight never answers for a film (decision 546). -->
  <RatePeek title={peek} onClose={() => (peek = null)} />
{/if}

<ActionSheet
  open={ending}
  title="End this room for everyone?"
  options={[{ label: 'End the room', destructive: true, onSelect: endTheRoom }]}
  onClose={() => (ending = false)}
/>

<style>
  .tonight {
    display: flex;
    flex-direction: column;
  }
  /* Over the tab bar and the top row: a room, or solo's round, is a flow until it resolves. */
  .flow {
    position: fixed;
    inset: 0;
    z-index: 55;
    overflow-y: auto;
    overscroll-behavior: contain;
    background: var(--bg);
    padding: env(safe-area-inset-top) max(var(--gutter), env(safe-area-inset-right)) 0
      max(var(--gutter), env(safe-area-inset-left));
    --enter-y: 16px;
    animation: enter var(--dur-slow) var(--ease);
  }
  .screen {
    width: 100%;
    max-width: 560px;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .at-door {
    padding-top: 8px;
  }
  .flow .screen {
    min-height: 100%;
    margin: 0 auto;
    gap: 20px;
    padding-bottom: calc(16px + env(safe-area-inset-bottom));
    --enter-y: 0;
    animation: enter 180ms var(--ease);
  }
  .stack {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  p {
    margin: 0;
  }
  .figures {
    font-variant-numeric: tabular-nums;
  }
  .center {
    text-align: center;
  }
  .wide {
    width: 100%;
    min-height: 50px;
  }
  .error {
    color: var(--negative);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .footnote.link {
    word-break: break-all;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
    background: currentColor;
  }
  .chev {
    display: grid;
    color: rgba(245, 240, 232, 0.35);
  }
  .tile {
    flex: none;
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border-radius: 8px;
    color: #fff;
  }
  .tile.blue { background: #3d6fb6; }
  .tile.ember { background: var(--accent); }
  .tile.graphite { background: #6b635b; }
  .row-text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    text-align: left;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .rows > li + li {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .capsule {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 34px;
    min-height: 34px;
    padding: 0 14px;
    border: none;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    color: var(--text);
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .capsule.tinted {
    background: var(--accent-tint);
    color: var(--accent-text);
  }

  /* The door. */
  .fork {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .doors {
    display: grid;
    gap: 12px;
  }
  .door {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px 12px 16px 16px;
    border: none;
    border-radius: var(--r-md);
    background: var(--surface-1);
    color: var(--text);
    text-align: left;
  }
  .door-shape {
    display: block;
    height: 140px;
    border-radius: var(--r-md);
  }
  .spinner {
    width: 16px;
    height: 16px;
    border: 2px solid var(--text-2);
    border-right-color: transparent;
    border-radius: var(--r-pill);
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(1turn);
    }
  }
  .door-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .door .tile {
    width: 28px;
    height: 28px;
  }
  .door-name {
    font-size: var(--fs-section);
    line-height: 25px;
    font-weight: 600;
  }
  .summary {
    min-height: 60px;
    padding-right: 12px;
  }
  .change {
    color: var(--accent-text);
  }
  .join {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 52px;
    padding: 0 8px 0 16px;
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  /* The `:not()`s outrank design.css's field fill: here the row is the field. */
  .join input:not([type='checkbox']):not([type='radio']) {
    flex: 1;
    min-width: 0;
    padding: 0;
    background: none;
  }
  .room,
  .seat,
  .pick {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 60px;
    padding: 8px 8px 8px 16px;
  }
  .seat {
    padding-right: 16px;
  }

  /* Solo. */
  .back {
    align-self: flex-start;
    gap: 2px;
    margin-left: -10px;
    padding-left: 2px;
  }
  .solo {
    gap: 16px;
  }
  .solo-head {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin-top: -12px;
  }
  .picks {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .rise {
    --enter-y: 10px;
    animation: enter var(--dur-base) var(--ease) calc(var(--i, 0) * 70ms) backwards;
  }
  .hero {
    display: flex;
    gap: 12px;
    padding: 12px;
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .hero-art {
    flex: none;
    width: 120px;
  }
  .hero-text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 12px;
  }
  .hero-actions {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .hero-actions .play {
    width: 100%;
    min-height: 50px;
  }
  .thumb {
    flex: none;
    width: 56px;
  }
  .thumb.small {
    width: 40px;
  }
  .pick-name {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .label {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
    color: var(--text-2);
  }
  .play {
    text-decoration: none;
  }
  .play:hover {
    color: var(--on-accent);
  }
  .play-icon {
    flex: none;
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    padding: 0;
    border: none;
    background: none;
    color: var(--accent-text);
  }
  .play-icon:disabled {
    color: var(--text-3);
    opacity: 0.5;
    cursor: default;
  }
  /* A room's own bar, in place of the tab bar's frame. */
  .bar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    min-height: 44px;
    margin: 0 -8px;
  }
  .bar .back {
    margin-left: 0;
  }
  .bar-title,
  .bar-count {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .bar-count {
    font-size: var(--fs-subhead);
    font-weight: 400;
    color: var(--text-2);
  }
  .bar-end {
    justify-self: end;
  }
  .beat {
    padding: 0;
  }
  .done {
    font-weight: 600;
  }
  .dock {
    position: sticky;
    bottom: 0;
    z-index: 1;
    margin: auto calc(-1 * var(--gutter)) calc(-16px - env(safe-area-inset-bottom));
    padding: 12px var(--gutter) calc(12px + env(safe-area-inset-bottom));
    background: var(--bar);
    -webkit-backdrop-filter: blur(24px) saturate(1.5);
    backdrop-filter: blur(24px) saturate(1.5);
    box-shadow: inset 0 0.5px 0 var(--separator);
  }

  /* The lobby. */
  .code-card {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: 20px;
    border-radius: var(--r-md);
    background: var(--surface-1);
    text-align: center;
  }
  .code-card .list-header {
    padding: 0;
  }
  .room-code {
    font-size: var(--fs-display);
    line-height: 48px;
    font-weight: 600;
    letter-spacing: 0.04em;
    font-variant-numeric: tabular-nums;
  }
  .code-card .capsule {
    margin-top: 8px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 12px 16px;
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .chips .pill:disabled {
    opacity: 0.45;
    cursor: default;
  }

  /* The round, a room's seat's or solo's. */
  .round {
    gap: 16px;
  }
  .dots {
    display: flex;
    justify-content: center;
    gap: 6px;
  }
  .dots span {
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
    background: rgba(245, 240, 232, 0.16);
  }
  .dots span.on {
    background: var(--text);
  }
  /* Two lines on a phone, as the round's board sets it. */
  .question {
    max-width: 8em;
    margin: 4px auto 0;
    text-align: center;
    text-wrap: balance;
  }
  /* Two columns at every width: stacked, the second option fell below the fold. */
  .pair {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .choice {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    min-width: 0;
    color: var(--text);
    text-align: center;
  }
  /* Only the posters and their words move: a button's own box never does. */
  .choice :is(.art, .choice-name, .fact) {
    transition: transform 180ms var(--ease), opacity 180ms var(--ease), filter 180ms var(--ease),
      box-shadow 180ms var(--ease);
  }
  .choice .art {
    position: relative;
    display: block;
    width: min(100%, 200px);
    margin-bottom: 8px;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: none;
  }
  .choice .art:active {
    transform: scale(var(--press));
    transition-duration: var(--dur-press);
  }
  .info {
    position: absolute;
    top: 8px;
    right: 8px;
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    border-radius: var(--r-pill);
    background: rgba(12, 11, 10, 0.72);
    color: var(--text);
  }
  .choice.up .art {
    box-shadow: 0 0 0 2px var(--text);
  }
  .choice.down :is(.art, .choice-name, .fact) {
    opacity: 0.35;
  }
  .choice.down .art {
    filter: saturate(0.5);
  }
  @media (prefers-reduced-motion: no-preference) {
    .choice.up .art {
      transform: translateY(-6px) scale(1.03);
    }
    .choice.down .art {
      transform: scale(0.95);
    }
  }
  .pair :is(.art, .choice-name, .fact) {
    --enter-y: 14px;
    animation: enter 260ms var(--ease) backwards;
  }
  .choice + .choice :is(.art, .choice-name, .fact) {
    animation-delay: 60ms;
  }
  .choice-name {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .fact {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .answers {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .answers .btn-secondary {
    min-height: 48px;
    padding: 0 12px;
    line-height: 20px;
    transition: background-color 140ms var(--ease), color 140ms var(--ease),
      opacity 140ms var(--ease);
  }
  /* The answer given takes the light fill; the rest step back. */
  .answers .held {
    background: var(--text);
    color: var(--bg);
    font-weight: 600;
  }
  .answers .held:disabled {
    opacity: 1;
  }
  .others {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .others li {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .blind {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    text-align: center;
  }
  /* At the foot of the round, the escape beside the host's End the room. */
  .escape {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    justify-items: center;
    align-items: center;
    gap: 12px;
    margin-top: auto;
    text-align: center;
  }
  .locked {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    color: var(--text-3);
  }
  /* A desktop window is short for its width: one line of question, the round's own 16px gaps, and
     posters from the height the other rows leave (462px, the first-pair line included). */
  @media (min-width: 721px) {
    .flow .round {
      gap: 16px;
    }
    .question {
      max-width: none;
    }
    .choice .art {
      width: clamp(
        120px,
        (100dvh - 462px - env(safe-area-inset-top) - env(safe-area-inset-bottom)) * 2 / 3,
        200px
      );
    }
  }

  /* The ballot. */
  .whose {
    font-size: var(--fs-subhead);
    color: var(--text-2);
  }
  .option {
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
    min-height: 60px;
    padding: 8px 16px 8px 12px;
    border: none;
    background: none;
    color: var(--text);
    text-align: left;
  }
  .tick {
    flex: none;
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    border-radius: var(--r-pill);
    box-shadow: inset 0 0 0 1.5px rgba(245, 240, 232, 0.28);
    color: var(--on-accent);
  }
  .option[aria-pressed='true'] .tick {
    --press: 0.85;
    background: var(--accent);
    box-shadow: none;
    animation: pop 240ms var(--ease-spring);
  }

  /* The reveal. */
  .reveal .bar {
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  }
  .result {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .playing .winner-art {
    animation: develop 600ms var(--ease) backwards;
  }
  .playing .winner h2 {
    --enter-y: 8px;
    animation: enter 260ms var(--ease) 300ms backwards;
  }
  .playing :is(.winner-meta, .label) {
    --enter-y: 4px;
    animation: enter 240ms var(--ease) 400ms backwards;
  }
  .playing .approval {
    animation: enter 200ms var(--ease) 500ms backwards;
  }
  .playing .approval :global(.yes) {
    animation: land 280ms var(--ease-spring) 500ms backwards;
  }
  .playing .dock {
    --enter-y: 100%;
    animation: enter var(--dur-slow) var(--ease) 640ms backwards;
  }
  .playing .lines {
    animation: enter 200ms var(--ease) 700ms backwards;
  }
  @keyframes develop {
    from {
      filter: brightness(0.25) saturate(0.6);
      transform: scale(0.965);
    }
  }
  @keyframes land {
    from {
      transform: scale(0);
    }
  }
  .winner {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    text-align: center;
  }
  /* Bounded, so Play on Jellyfin stays close to a phone's first screen. */
  .winner-art {
    display: block;
    width: min(200px, 26dvh);
    margin-bottom: 8px;
  }
  .winner-meta {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  .approval {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    margin-top: 4px;
    font-size: var(--fs-body);
    font-weight: 600;
  }
  .faces {
    display: flex;
    gap: 6px;
  }
  .sentence::first-letter {
    text-transform: uppercase;
  }

  /* The settings sheet. */
  .sheet-bar {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    margin: -4px -8px 8px;
  }
  .sheet-bar h2 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .sheet-bar .done {
    justify-self: end;
  }
  .settings {
    display: flex;
    flex-direction: column;
    gap: 24px;
    padding-top: 8px;
  }
  .setting {
    border-radius: var(--r-md);
    background: var(--surface-1);
    padding: 0 16px;
    min-height: 52px;
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .setting-line {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .how-long {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px 16px 12px;
  }
  .grow {
    flex: 1;
  }
  .readout {
    color: var(--text-2);
  }
  .range {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 28px;
    margin: 0;
    background: transparent;
  }
  .range::-webkit-slider-runnable-track {
    height: 4px;
    border-radius: 2px;
    background: linear-gradient(
      to right,
      var(--accent) var(--fill),
      rgba(245, 240, 232, 0.16) var(--fill)
    );
  }
  .range::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 28px;
    height: 28px;
    margin-top: -12px;
    border-radius: var(--r-pill);
    background: var(--text);
    box-shadow: var(--shadow-menu);
  }
  .range::-moz-range-track {
    height: 4px;
    border-radius: 2px;
    background: rgba(245, 240, 232, 0.16);
  }
  .range::-moz-range-progress {
    height: 4px;
    border-radius: 2px;
    background: var(--accent);
  }
  .range::-moz-range-thumb {
    width: 28px;
    height: 28px;
    border: none;
    border-radius: var(--r-pill);
    background: var(--text);
    box-shadow: var(--shadow-menu);
  }
  .stepper {
    flex: none;
    display: flex;
    align-items: center;
    width: 94px;
    height: 32px;
    border-radius: 8px;
    background: var(--surface-2);
  }
  .stepper button {
    flex: 1;
    height: 100%;
    display: grid;
    place-items: center;
    padding: 0;
    border: none;
    background: none;
    color: var(--text);
  }
  .stepper button:disabled {
    color: var(--text-3);
    cursor: default;
  }
  .stepper .sep {
    flex: none;
    width: 0.5px;
    height: 18px;
    background: var(--separator);
  }
</style>
