<script>
  /**
   * The TV kiosk route. Spec v2.1 §6.2 step 8 (v2.1 numbering), §12's M4 row ("+ TV route").
   *
   *   "Optional **TV kiosk route** (`/tv`, room code): lobby, progress, result."
   *
   * Three states and no controls. The TV is the one screen everybody in the room can see, so
   * the blind rule has to hold here before it is worth anything on the phones — and it does,
   * structurally: this page reads the same routes the phones read, and `ballot.tally` refuses
   * the result until every seat has submitted. There is nothing to enforce here because there
   * is nothing here that could see an answer.
   *
   * Deliberately read-only. A television is a shared device with no signed-in person at it; a
   * control on this screen would be an anonymous write, and §6.2 gives it none.
   */
  import { onDestroy, onMount } from 'svelte';
  import { ApiError, get } from '$lib/api.js';
  import { progressLine, approvalShare, REVEAL_BEAT } from '$lib/tonight.svelte.js';

  let code = $state('');
  let lobby = $state(null);
  let result = $state(null);
  let error = $state('');
  let timer;

  /** The TV has no keyboard worth using, so the code arrives in the URL when the phone hands it
   * over — `/tv?code=MX-2210` — and by typing only as a fallback. */
  onMount(() => {
    const fromUrl = new URLSearchParams(location.search).get('code');
    if (fromUrl) {
      code = fromUrl;
      attach();
    }
    // Polled rather than socketed: a kiosk has no session cookie, so it cannot authenticate the
    // channel — and a screen that is a minute stale is a screen, not a bug.
    timer = setInterval(() => lobby && attach(), 4000);
  });
  onDestroy(() => clearInterval(timer));

  async function attach() {
    try {
      const rooms = await get('/tonight/rooms');
      const room = rooms.rooms.find((r) => r.room_code.toUpperCase() === code.toUpperCase());
      if (!room) {
        error = 'no live room has that code';
        lobby = null;
        return;
      }
      error = '';
      lobby = await get(`/tonight/sessions/${room.session_id}`);
      if (lobby.ballot?.revealed) {
        result = await get(`/tonight/sessions/${room.session_id}/result`);
      } else {
        result = null;
      }
    } catch (err) {
      // A 409 is the blind rule holding, not a failure: somebody has not submitted yet.
      if (!(err instanceof ApiError && err.status === 409)) {
        error = err instanceof ApiError ? err.detail?.message || err.message : 'not reachable';
      }
    }
  }
</script>

<section data-testid="tv-surface">
  {#if !lobby}
    <form
      onsubmit={(e) => {
        e.preventDefault();
        attach();
      }}
    >
      <p class="data label">ROOM CODE</p>
      <input bind:value={code} placeholder="MX-2210" aria-label="room code" data-testid="tv-code" />
      <button class="pill" type="submit" data-testid="tv-attach">Show</button>
      {#if error}<p class="why" data-testid="tv-error">{error}</p>{/if}
    </form>
  {:else if result}
    <div data-testid="tv-result">
      <p class="data beat">{REVEAL_BEAT}</p>
      <h1>{result.winner?.name}</h1>
      <p class="data" data-testid="tv-approval">{approvalShare(result)}</p>
    </div>
  {:else if lobby.state === 'open'}
    <div data-testid="tv-lobby">
      <p class="data code">{lobby.room_code}</p>
      <ul>
        {#each lobby.seats as seat (seat.participant_id)}<li>{seat.name}</li>{/each}
      </ul>
      <p class="why">Waiting for {lobby.host?.name} to start.</p>
    </div>
  {:else}
    <div data-testid="tv-progress">
      <p class="data code">{lobby.room_code}</p>
      <p class="data">{progressLine(lobby.progress ?? [])}</p>
      <p class="why">Nobody's answers appear here, or anywhere, until every round has finished.</p>
    </div>
  {/if}
</section>

<style>
  section {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 18px; min-height: 60vh; text-align: center;
  }
  h1 { margin: 0; font-size: 34px; font-weight: 600; }
  .code { font-size: 40px; letter-spacing: 0.2em; color: var(--ember); }
  .beat { letter-spacing: 0.2em; color: var(--ember); font-size: 14px; }
  .label { letter-spacing: 0.14em; color: var(--ink-4); font-size: 11px; }
  .data { font-family: var(--mono); color: var(--ink-2); font-size: 16px; }
  .why { color: var(--ink-3); font-size: 14px; }
  ul { list-style: none; margin: 0; padding: 0; font-size: 20px; }
  input { min-height: var(--touch); font-size: 20px; text-align: center; }
</style>
