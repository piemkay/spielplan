<script>
  /**
   * §6.6's spend guard, the half that is not an estimate: "monthly cap, running meter reading
   * '$4.12 of $25.00 this month'". Spec v2.1 §6.6, §9, §8 stage 6; decisions 325, 343, 436, 452.
   *
   * The meter is `spend.meter`'s SUM over `llm_call`, read and never counted here (decision 325):
   * a counter kept on a page drifts, and a drifted cap is not a cap. "This month" is the calendar
   * month in the install's TZ, so the caption names the month, its end and the zone rather than
   * leaving the reader to guess whose midnight it turns over at.
   *
   * THE CAP IS WRITTEN IN PLACE AND IN FORCE AT ONCE (decision 452, and proposal 107's "editable in
   * place and takes effect immediately" as decision 330 adopts it). It enables no provider, so it
   * takes no preview. A finite number of at least zero; zero is a real cap meaning "spend nothing"
   * (decision 325); and there is no way back to "no cap" from here, because an unset cap parks every
   * title under decision 348's sentence -- a state an install starts in, not one an admin chooses.
   *
   * THREE THINGS THIS CARD ALWAYS SAYS, because nowhere else on the surface can. The thinking-token
   * note, because §9's fivefold undercount is invisible to an operator anywhere else (plan A4). The
   * unsettled share apart from the settled one, because a month spent on calls whose answer never
   * arrived is held at their ceilings and must not read as answers (decision 436). And, at the cap,
   * what stage 6 does about it and what an admin retry meets, so the park is not a silence.
   *
   * It never prints §6.6's corpus baseline per title and pass: that figure rests on the Gemini 2.5
   * family, which new keys can no longer call, and is three to six times low against the current
   * default (decision 343). The figures on this page are the price table's, and each says which.
   */
  import { saveCap, spend, usd } from '$lib/spendGuard.svelte.js';

  // `spend.ATTEMPTS`: attempt 2 is reserved inside the cap before attempt 1 is sent (decision 325).
  const ATTEMPTS = 2;

  let amount = $state('');

  const meter = $derived(spend.llm?.meter ?? null);
  const capped = $derived(meter?.cap_usd !== null && meter?.cap_usd !== undefined);
  // `remaining_usd` is floored at zero by `spend.meter`, so "at the cap" is read off the two
  // figures it is made of: a month that overshot is over the cap, not at a remaining of $0.00.
  const over = $derived(capped && Number(meter.spent_usd) >= Number(meter.cap_usd));
  // And at the cap before the meter reaches it. `spend.cap_check` parks a title once the month plus
  // that title's reservation -- `spend.ATTEMPTS` attempts of every run, decision 325's retry budgeted
  // inside -- would pass the cap; the gate admits nothing that would, so spend stops short of the
  // cap and this band, not spent >= cap, is how a capped month normally ends. Waiting for the
  // meter left the guard reading "$0.02 left" with no alert while every title parked.
  //
  // The reservation here is the stored plan's figure at the estimate's spec-size pack; the gate
  // counts each title's real pack at the dearer of today's and tomorrow's price, so a short pack
  // can still fit where this says none will. Compared in whole micro-dollars, the unit the route
  // spells money in, so the boundary is the gate's `>` and not a float's rounding. Nothing is
  // said for a plan the gate cannot price: stage 6 parks that under the plan's own reason.
  // [M5.7 review cycle 1, M57-THESIS-02]
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

  /** The local month the meter sums over, named in the install's zone and not the browser's. */
  function month(iso, tz) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        month: 'long',
        year: 'numeric',
        timeZone: tz
      });
    } catch {
      // A zone this browser does not know: the month in the browser's own, which is the same
      // month on every install whose TZ and household share a country.
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
      <!-- §8: "paid stages (6) never auto-retry past the spend cap". The reason is the board's
           own word for it, and the retry clause is decision 330's as M5.6 adopts proposal 107:
           the refusal is M5.6's route, and this card is where an admin learns it is coming. -->
      <p class="alert" role="alert" data-meter-over-cap>
        <strong>over spend cap</strong>: stage 6 parks new extractions with that reason until the
        month rolls over on {day(meter.period_end, meter.tz)} or the cap is raised, and an admin
        retry that would breach it is refused with the same reason.
      </p>
    {:else if noRoom}
      <!-- The same park and the same refusal, reached with money left (M57-THESIS-02): what is
           left is less than one title reserves, so the gate refuses before a call is made. -->
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
        <!-- The raw text and not `bind:value`: Svelte binds an emptied number field as null,
             and `Number(null)` is 0 -- an empty box would read as "spend nothing". -->
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

    <!-- §9: "counting visible JSON understates cost ~5x" on Gemini, which bills its reasoning as
         output. Carried on every reading rather than behind a toggle (plan A4). -->
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
