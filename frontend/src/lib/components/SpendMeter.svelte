<script>
  // The meter is the server's SUM over `llm_call`, never counted here (decision 325). A cap takes
  // effect at once, and there is no way back to "no cap": an unset cap parks every title.
  import { saveCap, spend, usd } from '$lib/spendGuard.svelte.js';

  // `spend.ATTEMPTS`: attempt 2 is reserved inside the cap before attempt 1 is sent (decision 325).
  const ATTEMPTS = 2;

  let amount = $state('');

  const meter = $derived(spend.llm?.meter ?? null);
  const capped = $derived(meter?.cap_usd !== null && meter?.cap_usd !== undefined);
  // `remaining_usd` is floored at zero, so read "at the cap" off spent and cap.
  const over = $derived(capped && Number(meter.spent_usd) >= Number(meter.cap_usd));
  // The gate parks a title once the month plus its reservation would pass the cap, so a capped month
  // normally ends here, short of it. Micro-dollars, the route's unit, so `<` matches the gate's `>`.
  const micro = (amount) => Math.round(Number(amount) * 1e6);
  const perTitle = $derived(spend.llm?.estimate?.per_title_usd ?? 'unknown');
  const reserve = $derived(
    perTitle === 'unknown' || !Number.isFinite(Number(perTitle)) ? null : micro(perTitle) * ATTEMPTS
  );
  const noRoom = $derived(
    capped && !over && reserve !== null && micro(meter.remaining_usd) < reserve
  );
  const unsettled = $derived(meter && Number(meter.unsettled_usd) > 0);
  const typed = $derived(amount.trim() === '' ? null : Number(amount));
  const valid = $derived(typed !== null && Number.isFinite(typed) && typed >= 0);

  /** The month the meter sums over, in the install's zone, not the browser's. */
  function month(iso, tz) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        month: 'long',
        year: 'numeric',
        timeZone: tz
      });
    } catch {
      // A zone this browser does not know: fall back to the browser's own.
      return new Date(iso).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    }
  }

  function day(iso, tz) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: tz });
    } catch {
      return new Date(iso).toLocaleDateString();
    }
  }

  async function setCap() {
    if (!valid) return;
    if (await saveCap(typed)) amount = '';
  }
</script>

<section
  class="card"
  data-testid="spend-meter"
  data-meter-state={!meter
    ? 'unread'
    : !capped
      ? 'no-cap'
      : over
        ? 'over-cap'
        : noRoom
          ? 'no-room'
          : 'under-cap'}
>
  <h2>Spend guard</h2>
  {#if spend.llmError}<p class="err" role="alert">{spend.llmError}</p>{/if}
  {#if meter}
    <div class="data-lg reading" data-meter-reading>
      {#if capped}{usd(meter.spent_usd)} of {usd(meter.cap_usd)} this month{:else}{usd(
          meter.spent_usd
        )} this month · no cap{/if}
    </div>
    <div class="data">
      {month(meter.period_start, meter.tz)} · until {day(meter.period_end, meter.tz)} · {meter.tz}
      {#if capped && !over}· {usd(meter.remaining_usd)} left{/if}
    </div>
    {#if unsettled}
      <p class="why" data-meter-unsettled>
        Of that, <span class="data">{usd(meter.unsettled_usd)}</span> is calls whose answer never
        arrived, held at the most they could have billed until the provider reports otherwise
        (decision 436).
      </p>
    {/if}

    {#if !capped}
      <p class="alert" data-meter-no-cap>
        No cap is set, so stage 6 parks every title and bills nothing until one is set.
      </p>
    {:else if over}
      <p class="alert" role="alert" data-meter-over-cap>
        <strong>over spend cap</strong>: stage 6 parks new extractions with that reason until the
        month rolls over on {day(meter.period_end, meter.tz)} or the cap is raised, and an admin
        retry that would breach it is refused with the same reason.
      </p>
    {:else if noRoom}
      <p class="alert" role="alert" data-meter-over-cap>
        <strong>over spend cap</strong> for the next title: the {usd(meter.remaining_usd)} left is
        less than the {usd(reserve / 1e6)} one title reserves at the estimate ({ATTEMPTS} attempts),
        so stage 6 parks titles with that reason until the month rolls over on
        {day(meter.period_end, meter.tz)} or the cap is raised, and an admin retry that would breach
        it is refused with the same reason.
      </p>
    {/if}

    <div class="cap">
      <label>
        <span class="data">MONTHLY CAP · USD</span>
        <!-- Raw text, not bind:value: an emptied number field binds null, and Number(null) is 0. -->
        <input
          type="number"
          min="0"
          step="0.01"
          inputmode="decimal"
          value={amount}
          oninput={(e) => (amount = e.currentTarget.value)}
          placeholder={capped ? usd(meter.cap_usd) : 'e.g. 5.00'}
        />
      </label>
      <button
        class="btn-primary"
        onclick={setCap}
        disabled={!valid || spend.busy === 'cap'}
      >
        {spend.busy === 'cap' ? 'Setting…' : 'Set cap'}
      </button>
    </div>
    <p class="why">
      In force the moment it is set. 0 is a cap too: it spends nothing.
    </p>
    {#if spend.capError}<p class="err" role="alert">{spend.capError}</p>{/if}

    <p class="why" data-meter-caption>
      Metered as billed: every call is written before it is sent and settled to what the provider
      reported, thinking tokens included. Gemini bills its reasoning as output, so counting only
      the visible answer would understate the cost about fivefold (§9).
    </p>
  {:else if !spend.llmError}
    <p class="data">loading…</p>
  {/if}
</section>

<style>
  h2 {
    margin: 0 0 6px;
    font-size: 15px;
    font-weight: 600;
  }
  .card {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .reading {
    font-size: 15px;
    color: var(--ink);
  }
  .cap {
    display: flex;
    gap: 8px;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .cap label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    flex: 1;
    min-width: 160px;
  }
  .alert {
    margin: 0;
    padding: 8px 10px;
    border: 1px solid var(--ember-lift);
    border-radius: var(--r-sm);
    color: var(--ember-lift);
    font-size: 12.5px;
    line-height: 1.45;
  }
  .why {
    margin: 0;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
    margin: 0;
  }
</style>
