<script>
  // The meter is the server's SUM over `llm_call`, never counted here (decision 325). A cap takes
  // effect at once, and there is no way back to "no cap": an unset cap parks every title.
  import { about, runOutDays, saveCap, spend, usd } from '$lib/spendGuard.svelte.js';

  // `spend.ATTEMPTS`: attempt 2 is reserved inside the cap before attempt 1 is sent (decision 325).
  const ATTEMPTS = 2;

  let amount = $state('');
  let editing = $state(false);

  const meter = $derived(spend.llm?.meter ?? null);
  const projected = $derived(spend.llm?.projected ?? null);
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
  const runsOut = $derived(capped && !over && !noRoom && runOutDays(spend.llm) !== null);
  const attention = $derived(Boolean(meter) && (!capped || over || noRoom || runsOut));
  const share = $derived(
    !capped
      ? 0
      : Number(meter.cap_usd) > 0
        ? Math.min(100, (100 * Number(meter.spent_usd)) / Number(meter.cap_usd))
        : 100
  );
  const unsettled = $derived(meter && Number(meter.unsettled_usd) > 0);
  const typed = $derived(amount.trim() === '' ? null : Number(amount));
  const valid = $derived(typed !== null && Number.isFinite(typed) && typed >= 0);

  /** A date of the month the meter sums over, in the install's zone, not the browser's. */
  function inZone(iso, tz, options) {
    if (!iso) return '';
    // English like the sentence it sits in.
    try {
      return new Date(iso).toLocaleDateString('en-GB', { ...options, timeZone: tz });
    } catch {
      // A zone this browser does not know: fall back to the browser's own.
      return new Date(iso).toLocaleDateString('en-GB', options);
    }
  }
  const resets = $derived(
    meter ? inZone(meter.period_end, meter.tz, { day: 'numeric', month: 'long' }) : ''
  );
  const nextMonth = $derived(meter ? inZone(meter.period_end, meter.tz, { month: 'long' }) : '');

  async function setCap() {
    if (!valid) return;
    if (await saveCap(typed)) {
      amount = '';
      editing = false;
    }
  }

  function stopEditing() {
    editing = false;
    amount = '';
    spend.capError = '';
  }
</script>

{#snippet warning(title)}
  <div class="head">
    <span class="tile" aria-hidden="true">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4 2.8 19.5h18.4z" /><path d="M12 10v4.5M12 17.2v.01" /></svg>
    </span>
    <h2>{title}</h2>
  </div>
{/snippet}

<div
  class="meter"
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
  {#if spend.llmError}<p class="err" role="alert">{spend.llmError}</p>{/if}
  {#if meter}
    <section class="card reading" aria-label="Spent this month">
      <p class="figure" data-meter-reading>
        <span class="spent">{usd(meter.spent_usd)}</span>
        <span class="of">{capped ? `of ${usd(meter.cap_usd)} this month` : 'this month, no cap set'}</span>
      </p>
      {#if capped}
        <div class="track" aria-hidden="true">
          <div class="fill" class:warn={attention} style:width="{share}%"></div>
        </div>
      {/if}
      <p class="footnote">
        Resets {resets}.{#if capped && !over}{' '}{usd(meter.remaining_usd)} left.{/if}
      </p>
      {#if unsettled}
        <p class="footnote" data-meter-unsettled>
          Of that, {usd(meter.unsettled_usd)} is calls whose answer never arrived, held at the most
          they could cost until the provider reports otherwise.
        </p>
      {/if}
    </section>

    {#if !capped}
      <article class="card attention" data-meter-no-cap>
        {@render warning('No cap is set')}
        <p>New titles wait, and nothing is spent, until a monthly cap is set.</p>
      </article>
    {:else if over}
      <article class="card attention" role="alert" data-meter-over-cap>
        {@render warning("This month's budget is spent")}
        <p>
          New titles wait, marked “over spend cap”, until {resets} or until the cap is raised. An
          admin retry that would pass the cap is refused the same way.
        </p>
      </article>
    {:else if noRoom}
      <article class="card attention" role="alert" data-meter-over-cap>
        {@render warning('Not enough left for the next title')}
        <p>
          {usd(meter.remaining_usd)} is left, and one title holds {usd(reserve / 1e6)} for its
          {ATTEMPTS} attempts. New titles wait, marked “over spend cap”, until {resets} or until the
          cap is raised. An admin retry that would pass the cap is refused the same way.
        </p>
      </article>
    {:else if runsOut}
      <article class="card attention" data-meter-runs-out>
        {@render warning(`About ${about(projected.monthly_usd)} a month at this pace`)}
        <p>
          {projected.titles} new title{projected.titles === 1 ? '' : 's'} arrived in the last
          {projected.window_days} days. Once {usd(meter.cap_usd)} is spent, new titles wait until
          {nextMonth}.
        </p>
      </article>
    {/if}

    {#if editing}
      <div class="card cap">
        <label class="field">
          <span>Monthly cap, in dollars</span>
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
        <p class="footnote">In force the moment it's set. $0 is a cap too: it spends nothing.</p>
        {#if spend.capError}<p class="err" role="alert">{spend.capError}</p>{/if}
        <div class="actions">
          <button class="btn-primary" onclick={setCap} disabled={!valid || spend.busy === 'cap'}>
            {spend.busy === 'cap' ? 'Setting…' : 'Set cap'}
          </button>
          <button class="btn-secondary" onclick={stopEditing}>Cancel</button>
        </div>
      </div>
    {:else}
      <div class="actions wide">
        <button
          class={attention && !spend.proposal ? 'btn-primary' : 'btn-secondary'}
          data-testid="cap-open"
          onclick={() => (editing = true)}
        >
          {!capped ? 'Set a cap' : attention ? 'Raise the cap' : 'Change the cap'}
        </button>
        {#if attention}<a class="btn-secondary" href="#plan">Change the plan</a>{/if}
      </div>
    {/if}

    <p class="footnote caption" data-meter-caption>
      Costs are counted as billed, thinking tokens included: every call is written down before it's
      sent and settled to what the provider reports. Gemini bills its thinking as output, so counting
      only the visible answer would understate the cost about fivefold.
    </p>
  {:else if !spend.llmError}
    <p class="footnote">Loading…</p>
  {/if}
</div>

<style>
  .meter {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .reading,
  .attention,
  .cap {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .figure {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
    font-variant-numeric: tabular-nums;
  }
  .spent {
    font-size: var(--fs-large);
    line-height: 40px;
    font-weight: 600;
  }
  .of {
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .track {
    height: 8px;
    border-radius: var(--r-pill);
    background: var(--progress-track);
    overflow: hidden;
  }
  .fill {
    height: 100%;
    border-radius: var(--r-pill);
    background: var(--progress-fill);
  }
  .fill.warn {
    background: var(--warning);
  }
  .footnote {
    margin: 0;
    font-variant-numeric: tabular-nums;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .tile {
    width: 30px;
    height: 30px;
    flex: none;
    display: grid;
    place-items: center;
    border-radius: var(--r-poster);
    background: var(--warning-tint);
    color: var(--warning);
  }
  .head h2 {
    flex: 1;
    min-width: 0;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .attention p {
    margin: 0;
    font-size: var(--fs-callout);
    line-height: 21px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: var(--fs-footnote);
    color: var(--text-2);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .actions.wide {
    flex-direction: column;
    gap: 12px;
    padding-top: 4px;
  }
  .actions.wide > * {
    width: 100%;
  }
  .actions.wide > .btn-primary {
    min-height: 50px;
  }
  .caption {
    padding: 0 var(--gutter);
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }
</style>
