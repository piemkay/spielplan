/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, get, post } from '$lib/api.js';
import { cancel, spend } from '$lib/spendGuard.svelte.js';
import ExtractionSettings from './ExtractionSettings.svelte';
import LlmProviderCard from './LlmProviderCard.svelte';
import SourceConnectorCard from './SourceConnectorCard.svelte';
import SpendMeter from './SpendMeter.svelte';

const MODES = { anthropic: 'forced tool-use', openai: 'strict schema', gemini: 'responseSchema' };

const tableBasis = (provider, model) => ({
  provider,
  model,
  source: 'table',
  input: 0.75,
  output: 3.75,
  valid_until: '2027-01-01',
  then: { input: 1.5, output: 7.5 }
});

/** One `providers[]` entry of `GET /api/admin/llm`: booleans for the key, never a value. */
const provider = (name, over = {}) => ({
  name,
  configured: true,
  has_api_key: true,
  secrets_unreadable: false,
  model: `${name}-model`,
  structured_output: MODES[name],
  price: { input: 0.75, output: 3.75, valid_until: '2027-01-01' },
  models: [`${name}-model`, `${name}-other`],
  price_basis: tableBasis(name, `${name}-model`),
  ...over
});

const estimate = (over = {}) => ({
  per_title_usd: '0.032175',
  input_tokens_assumed: 23500,
  output_tokens_assumed: 3900,
  passes: 1,
  providers: ['gemini'],
  reason: null,
  basis: [tableBasis('gemini', 'gemini-model')],
  ...over
});

const projected = (over = {}) => ({
  window_days: 30,
  titles: 12,
  ever_filed: true,
  monthly_usd: '0.3861',
  remaining_usd: '20.88',
  exceeds_remaining: false,
  reason: null,
  ...over
});

const inDays = (n) => new Date(Date.now() + n * 86_400_000).toISOString();

const meter = (over = {}) => ({
  spent_usd: '4.12',
  unsettled_usd: '0',
  cap_usd: '25',
  remaining_usd: '20.88',
  period_start: '2026-09-01T00:00:00+02:00',
  period_end: '2026-10-01T00:00:00+02:00',
  tz: 'Europe/Berlin',
  ...over
});

/** `GET /api/admin/llm`, as `api/llm._read` answers it. */
const llm = (over = {}) => ({
  providers: [provider('anthropic'), provider('openai'), provider('gemini')],
  settings: {
    extraction_provider: 'gemini',
    parallel: null,
    parallel_providers: null,
    passes: null,
    cap_usd: 25
  },
  meter: meter(),
  estimate: estimate(),
  projected: projected(),
  batch: {
    available: false,
    reason: 'batch endpoints are not used at M5: every call is synchronous (decision 338)'
  },
  ...over
});

const preview = (over = {}) => ({
  estimate: estimate(),
  projected: projected(),
  meter: meter(),
  blocked: null,
  ...over
});

let target;
let apps = [];

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(get).mockResolvedValue(llm());
  cancel();
  spend.llm = llm();
  spend.error = '';
  spend.llmError = '';
});

afterEach(() => {
  for (const app of apps) unmount(app);
  apps = [];
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function show(Component, props = {}) {
  apps.push(mount(Component, { target, props }));
  await settle();
}

function button(label) {
  const found = [...target.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
  expect(found, `no button named ${label}`).toBeDefined();
  return found;
}

function field(text) {
  const label = [...target.querySelectorAll('label')].find((l) => l.textContent.includes(text));
  expect(label, `no field labelled ${text}`).toBeDefined();
  return label.querySelector('input, select');
}

/** A control by its accessible name, for the rows whose label is the whole row. */
function control(name) {
  const found = target.querySelector(`[aria-label="${name}"]`);
  expect(found, `no control named ${name}`).not.toBeNull();
  return found;
}

function type(input, value) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

/** A switch pressed the way a finger presses it. */
function toggle(input) {
  input.click();
  flushSync();
}

function commit(input, value) {
  type(input, value);
  input.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

describe('a provider card (plan B1-B3)', () => {
  it('never renders a stored key, masks it, and empties the field after a save', async () => {
    vi.mocked(api).mockResolvedValue({ name: 'gemini', has_api_key: true, secrets_unreadable: false });
    await show(LlmProviderCard, { card: provider('gemini') });
    const key = field('API key');
    expect(key.type).toBe('password');
    expect(key.value, 'nothing the server sent is bound to the field').toBe('');
    expect(key.placeholder).toBe('Saved');

    type(key, 'sk-live-not-a-real-key');
    button('Save key').click();
    await settle();
    expect(api).toHaveBeenCalledWith('/admin/connectors/gemini', {
      method: 'PUT',
      body: { api_key: 'sk-live-not-a-real-key' }
    });
    expect(key.value).toBe('');
    expect(target.innerHTML).not.toContain('sk-live-not-a-real-key');
  });

  it('says "Paste a key" when none is stored, and an empty field sends nothing', async () => {
    await show(LlmProviderCard, { card: provider('openai', { has_api_key: false, configured: false }) });
    expect(field('API key').placeholder).toBe('Paste a key');
    expect(button('Save key').disabled).toBe(true);
    button('Save key').click();
    await settle();
    expect(api).not.toHaveBeenCalled();
  });

  it('marks an un-configured provider as such and says which half is missing', async () => {
    await show(LlmProviderCard, { card: provider('anthropic', { has_api_key: false, configured: false }) });
    await show(LlmProviderCard, { card: provider('gemini') });
    const [bare, ready] = target.querySelectorAll('[data-provider]');
    expect(bare.getAttribute('data-configured')).toBe('false');
    expect(bare.querySelector('[data-unconfigured]').textContent).toContain('no key is saved');
    expect(ready.getAttribute('data-configured')).toBe('true');
    expect(ready.querySelector('[data-unconfigured]')).toBeNull();
  });

  it("captions each card with its adapter's structured-output mode", async () => {
    for (const name of ['anthropic', 'openai', 'gemini']) {
      await show(LlmProviderCard, { card: provider(name) });
    }
    const captions = [...target.querySelectorAll('[data-structured-output]')].map((c) => c.textContent);
    expect(captions[0]).toContain('forced tool-use');
    expect(captions[1]).toContain('strict schema');
    expect(captions[2]).toContain('responseSchema');
  });

  it('names the price it estimates at, and its date (decision 343)', async () => {
    await show(LlmProviderCard, { card: provider('gemini') });
    const caption = target.querySelector('[data-price-basis]').textContent;
    expect(caption).toContain('gemini-model');
    expect(caption).toContain('shipped table');
    expect(caption).toContain('valid until 2027-01-01, then $1.50 / $7.50');
  });

  it('reads an unpriced model as unknown and prints no figure for it', async () => {
    await show(LlmProviderCard, {
      card: provider('openai', { configured: false, price: 'unknown', price_basis: 'unknown' })
    });
    const caption = target.querySelector('[data-price-basis]');
    expect(caption.getAttribute('data-price-basis')).toBe('unknown');
    expect(caption.textContent).toContain('unknown');
    expect(caption.textContent).not.toContain('$');
    expect(target.querySelector('[data-unconfigured]').textContent).toContain('no price is known');
  });

  // The server sends `has_api_key: false` for a stored key it cannot decrypt.
  it('says a key that will not open is a restore or a retype, never that no key is stored', async () => {
    await show(LlmProviderCard, {
      card: provider('gemini', { has_api_key: false, configured: false, secrets_unreadable: true })
    });
    const alert = target.querySelector('[data-key-unreadable]');
    expect(alert.textContent).toContain('.env');
    expect(alert.textContent).toContain('paste the key');
    const unconfigured = target.querySelector('[data-unconfigured]').textContent;
    expect(unconfigured).not.toContain('no key is saved');
    expect(unconfigured).toContain("won't open");
    expect(field('API key').placeholder).not.toBe('Paste a key');
  });

  it('proposes a model change through the preview and never through the key route', async () => {
    vi.mocked(post).mockResolvedValue(preview());
    await show(LlmProviderCard, { card: provider('gemini') });
    commit(field('Model'), 'gemini-other');
    await settle();
    expect(post).toHaveBeenCalledWith('/admin/llm/preview', {
      providers: { gemini: { model: 'gemini-other' } }
    });
    expect(api, 'a model moves the estimate, so it never goes to PUT /connectors').not.toHaveBeenCalled();
    expect(target.querySelector('[data-provider-pending]')).not.toBeNull();
  });

  it('proposes a price override as a pair, and holds half of one without asking', async () => {
    vi.mocked(post).mockResolvedValue(preview());
    await show(LlmProviderCard, { card: provider('openai') });
    commit(field('Price in'), '2');
    await settle();
    expect(post).not.toHaveBeenCalled();
    expect(target.querySelector('[data-price-half]')).not.toBeNull();
    commit(field('Price out'), '8');
    await settle();
    expect(post).toHaveBeenCalledWith('/admin/llm/preview', {
      providers: { openai: { price_input: 2, price_output: 8 } }
    });
  });
});

describe('the extraction settings and their estimate (decisions 339, 450)', () => {
  it('offers one assignment and no other task slot', async () => {
    await show(ExtractionSettings);
    for (const name of ['anthropic', 'openai', 'gemini']) {
      await show(LlmProviderCard, { card: provider(name) });
    }
    const text = target.textContent.toLowerCase();
    expect(text).not.toContain('query parsing');
    expect(text).not.toContain('conflict phrasing');
    expect(target.querySelectorAll('[data-testid="llm-extraction"] select')).toHaveLength(2);
  });

  it('disables a provider with no key, saying to add one first', async () => {
    spend.llm = llm({
      providers: [
        provider('anthropic', { has_api_key: false, configured: false }),
        provider('openai'),
        provider('gemini')
      ]
    });
    await show(ExtractionSettings);
    const option = control('Provider').querySelector('option[value="anthropic"]');
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain('add a key first');
  });

  it("starts parallel mode and the pass count from the server's settings, not the page's", async () => {
    spend.llm = llm({
      settings: { ...llm().settings, parallel: true, parallel_providers: ['gemini', 'openai'] },
      estimate: estimate({ passes: 2 })
    });
    await show(ExtractionSettings);
    expect(control('Parallel mode').getAttribute('aria-checked')).toBe('true');
    expect(control('Run Gemini in parallel').checked).toBe(true);
    expect(control('Run Anthropic in parallel').checked).toBe(false);
    // An absent `passes` shows the count the server's own plan uses (decision 324 lives there).
    expect(control('Passes').value).toBe('2');
  });

  it('caption carries the consensus numbers, measured over providers, and that the merge counts runs', async () => {
    await show(ExtractionSettings);
    const text = target.textContent;
    expect(text).toContain('93%');
    expect(text).toContain('measured over providers');
    expect(text).toContain('merge counts runs');
  });

  it('shows the three figures before anything is stored, and confirms the one it showed', async () => {
    vi.mocked(post).mockResolvedValue(
      preview({
        estimate: estimate({ per_title_usd: '0.06435', passes: 2 }),
        projected: projected({ monthly_usd: '0.7722' })
      })
    );
    vi.mocked(api).mockResolvedValue(llm());
    await show(ExtractionSettings);
    const passes = control('Passes');
    passes.value = '2';
    passes.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    expect(post).toHaveBeenCalledWith('/admin/llm/preview', { passes: 2 });
    expect(api, 'the preview came first and wrote nothing').not.toHaveBeenCalled();

    const panel = target.querySelector('[data-testid="spend-estimate"]');
    expect(panel.getAttribute('data-estimate-state')).toBe('pending');
    expect(panel.querySelector('[data-per-title]').textContent).toContain('$0.06435');
    expect(panel.querySelector('[data-basis]').textContent).toContain('shipped table');
    expect(panel.querySelector('[data-projected]').textContent).toContain('$0.7722 a month');
    // Whitespace-collapsed: the sentences wrap across source lines to stay inside 108 columns.
    const said = panel.textContent.replace(/\s+/g, ' ');
    expect(said).toContain('$20.88 left');
    expect(said).toContain('23,500 tokens in and 3,900 billed out');

    button('Confirm').click();
    await settle();
    expect(api).toHaveBeenCalledWith('/admin/llm', {
      method: 'PUT',
      body: { passes: 2, accepted_estimate: '0.06435' }
    });
  });

  it('draws a month over the remaining cap as a warning, not as a figure', async () => {
    vi.mocked(post).mockResolvedValue(
      preview({ projected: projected({ monthly_usd: '31.20', exceeds_remaining: true }) })
    );
    await show(ExtractionSettings);
    toggle(control('Parallel mode'));
    await settle();
    const warning = target.querySelector('[data-projected-exceeds]');
    expect(warning).not.toBeNull();
    expect(warning.getAttribute('role')).toBe('alert');
    expect(target.querySelector('[data-projected]').getAttribute('data-exceeds')).toBe('true');
  });

  it('says there is no acquisition history rather than printing a guess', async () => {
    vi.mocked(post).mockResolvedValue(
      preview({ projected: projected({ ever_filed: false, titles: 0, monthly_usd: null }) })
    );
    await show(ExtractionSettings);
    toggle(control('Parallel mode'));
    await settle();
    const month = target.querySelector('[data-projected]');
    expect(month.getAttribute('data-projected')).toBe('no-history');
    expect(month.textContent).toContain('No titles have arrived yet');
    expect(month.textContent, 'no figure for a month').not.toMatch(/\$[\d.]+ a month/);
  });

  it('prints unknown and its reason for an unpriced plan, never a figure', async () => {
    vi.mocked(post).mockResolvedValue(
      preview({
        estimate: estimate({ per_title_usd: 'unknown', basis: ['unknown'], providers: ['openai'] }),
        projected: projected({ monthly_usd: 'unknown', exceeds_remaining: null })
      })
    );
    await show(ExtractionSettings);
    const select = control('Provider');
    select.value = 'openai';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    const perTitle = target.querySelector('[data-plan="pending"] [data-per-title]');
    expect(perTitle.textContent).toContain('Unknown');
    expect(perTitle.textContent).not.toContain('$');
    expect(target.querySelector('[data-unknown-reason]').textContent).toContain('OpenAI');
  });

  it('refuses to confirm a plan that names a provider with no usable key', async () => {
    vi.mocked(post).mockResolvedValue(preview({ blocked: 'no API key is configured for openai' }));
    await show(ExtractionSettings);
    const select = control('Provider');
    select.value = 'openai';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    expect(target.querySelector('[data-estimate-blocked]').textContent).toContain('openai');
    expect(button('Confirm').disabled).toBe(true);
    button('Confirm').click();
    await settle();
    expect(api).not.toHaveBeenCalled();
  });

  it('cancels with no request and puts the controls back as stored', async () => {
    vi.mocked(post).mockResolvedValue(preview());
    await show(ExtractionSettings);
    toggle(control('Parallel mode'));
    await settle();
    expect(control('Parallel mode').getAttribute('aria-checked')).toBe('true');
    vi.mocked(post).mockClear();
    vi.mocked(get).mockClear();
    button('Cancel').click();
    await settle();
    expect(post).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
    expect(api).not.toHaveBeenCalled();
    expect(target.querySelector('[data-testid="spend-estimate"]')).toBeNull();
    expect(control('Parallel mode').getAttribute('aria-checked')).toBe('false');
  });
});

describe('the spend meter (decisions 325, 343, 436, 452)', () => {
  it("reads the month's spend against the cap, and carries the thinking-token note", async () => {
    await show(SpendMeter);
    const card = target.querySelector('[data-testid="spend-meter"]');
    expect(card.getAttribute('data-meter-state')).toBe('under-cap');
    expect(card.querySelector('[data-meter-reading]').textContent).toContain('$4.12 of $25.00 this month');
    // The month ends in the install's zone, whatever the browser's.
    expect(card.textContent).toContain('Resets 1 October. $20.88 left.');
    expect(card.textContent).toContain('$20.88 left');
    expect(card.querySelector('[data-meter-runs-out]'), 'nothing runs out at this pace').toBeNull();
    expect(card.querySelector('[data-meter-caption]').textContent).toContain('thinking tokens');
    // §6.6's corpus baseline rests on a model family new keys cannot call (decision 343).
    expect(card.textContent).not.toContain('0.005');
  });

  it('shows the unsettled share apart from the settled spend (decision 436)', async () => {
    spend.llm = llm({ meter: meter({ unsettled_usd: '0.14' }) });
    await show(SpendMeter);
    expect(target.querySelector('[data-meter-unsettled]').textContent).toContain('$0.14');
  });

  it('says what happens at the cap, and what an admin retry meets', async () => {
    spend.llm = llm({ meter: meter({ spent_usd: '25.31', remaining_usd: '0' }) });
    await show(SpendMeter);
    const card = target.querySelector('[data-testid="spend-meter"]');
    expect(card.getAttribute('data-meter-state')).toBe('over-cap');
    const alert = card.querySelector('[data-meter-over-cap]').textContent.replace(/\s+/g, ' ');
    expect(alert).toContain('over spend cap');
    expect(alert).toContain('admin retry that would pass the cap is refused the same way');
    expect(button('Raise the cap').classList.contains('btn-primary')).toBe(true);
  });

  // The gate parks a title once the month plus its reservation would pass the cap, short of the cap.
  it('says over spend cap once what is left cannot hold one more title', async () => {
    // The stored plan's figure is $0.032175 a title and pass, so one title reserves $0.06435.
    spend.llm = llm({ meter: meter({ spent_usd: '24.98', remaining_usd: '0.02' }) });
    await show(SpendMeter);
    const card = target.querySelector('[data-testid="spend-meter"]');
    expect(card.getAttribute('data-meter-state')).toBe('no-room');
    const alert = card.querySelector('[data-meter-over-cap]');
    expect(alert, 'every title parks and the guard says nothing').not.toBeNull();
    const text = alert.textContent.replace(/\s+/g, ' ');
    expect(text).toContain('over spend cap');
    expect(text).toContain('$0.06435');
    expect(text).toContain('admin retry that would pass the cap is refused the same way');
    expect(card.textContent).toContain('$0.02 left');
  });

  it('keeps room for exactly one more title under the cap, and an unpriced plan too', async () => {
    // Step 5 refuses only `used + need > limit`: a month that ends on its cap has not passed it.
    spend.llm = llm({ meter: meter({ spent_usd: '24.93565', remaining_usd: '0.06435' }) });
    await show(SpendMeter);
    const card = target.querySelector('[data-testid="spend-meter"]');
    expect(card.getAttribute('data-meter-state')).toBe('under-cap');
    expect(card.querySelector('[data-meter-over-cap]')).toBeNull();
    for (const app of apps) unmount(app);
    apps = [];

    // A plan the gate cannot price parks under its own reason, which is not this card's to name.
    spend.llm = llm({
      meter: meter({ spent_usd: '24.98', remaining_usd: '0.02' }),
      estimate: estimate({ per_title_usd: 'unknown', reason: 'no price is known for gemini-x' })
    });
    await show(SpendMeter);
    const unpriced = target.querySelector('[data-testid="spend-meter"]');
    expect(unpriced.getAttribute('data-meter-state')).toBe('under-cap');
    expect(unpriced.querySelector('[data-meter-over-cap]')).toBeNull();
  });

  it('says new titles wait while no cap is set', async () => {
    spend.llm = llm({ meter: meter({ cap_usd: null, remaining_usd: null }) });
    await show(SpendMeter);
    expect(target.querySelector('[data-meter-no-cap]').textContent).toContain(
      'New titles wait, and nothing is spent'
    );
    expect(button('Set a cap').classList.contains('btn-primary')).toBe(true);
  });

  it('warns when the stored plan outruns what is left, from its own projection', async () => {
    spend.llm = llm({
      meter: meter({ spent_usd: '0.31', cap_usd: '2', remaining_usd: '1.69', period_end: inDays(20) }),
      projected: projected({
        titles: 889,
        monthly_usd: '28.667',
        remaining_usd: '1.69',
        exceeds_remaining: true
      })
    });
    await show(SpendMeter);
    const card = target.querySelector('[data-testid="spend-meter"]');
    expect(card.getAttribute('data-meter-state')).toBe('under-cap');
    const warning = card.querySelector('[data-meter-runs-out]').textContent.replace(/\s+/g, ' ');
    expect(warning).toContain('About $28.67 a month at this pace');
    expect(warning).toContain('889 new titles arrived in the last 30 days');
    expect(warning).toContain('Once $2.00 is spent, new titles wait until');
    expect(button('Raise the cap').classList.contains('btn-primary')).toBe(true);
    expect(target.querySelector('a[href="#plan"]').textContent).toBe('Change the plan');
  });

  it('stays quiet when the month ends before the pace spends what is left, as Overview does', async () => {
    spend.llm = llm({
      meter: meter({ spent_usd: '7', cap_usd: '10', remaining_usd: '3', period_end: inDays(2) }),
      projected: projected({ monthly_usd: '10', remaining_usd: '3', exceeds_remaining: true })
    });
    await show(SpendMeter);
    expect(target.querySelector('[data-meter-runs-out]'), 'nine days of room outlast the month').toBeNull();
    expect(button('Change the cap').classList.contains('btn-primary')).toBe(false);
  });

  it('sets a cap of zero as a real cap, and an empty field as nothing at all', async () => {
    vi.mocked(api).mockResolvedValue({ meter: meter({ cap_usd: '0' }) });
    await show(SpendMeter);
    expect(target.querySelector('input[type="number"]'), 'the cap opens on a tap').toBeNull();
    button('Change the cap').click();
    flushSync();
    const cap = field('Monthly cap');
    expect(button('Set cap').disabled, 'an empty field is not a cap of zero').toBe(true);
    type(cap, '0');
    button('Set cap').click();
    await settle();
    expect(api).toHaveBeenCalledWith('/admin/llm/cap', { method: 'PUT', body: { cap_usd: 0 } });
  });
});

describe('a source card (plan C, decision 453)', () => {
  it("says whether stage 2 needs it, and TMDB's key is the one it cannot do without", async () => {
    await show(SourceConnectorCard, {
      source: { name: 'tmdb', has_api_key: false, secrets_unreadable: false, required: true, used_by: 'tmdb:detail' }
    });
    const card = target.querySelector('[data-source="tmdb"]');
    expect(card.getAttribute('data-required')).toBe('true');
    expect(card.textContent).toContain('Required');
    expect(field('API key').placeholder).toBe('Paste a key');
  });

  it('takes a Trakt client id and secret, sends only what was typed, and empties both', async () => {
    vi.mocked(api).mockResolvedValue({ name: 'trakt', has_client_id: true, has_client_secret: false });
    await show(SourceConnectorCard, {
      source: {
        name: 'trakt',
        has_client_id: false,
        has_client_secret: false,
        secrets_unreadable: false,
        required: false,
        used_by: 'trakt'
      }
    });
    type(field('Client ID'), 'trakt-id-123');
    button('Save').click();
    await settle();
    expect(api).toHaveBeenCalledWith('/admin/connectors/trakt', {
      method: 'PUT',
      body: { client_id: 'trakt-id-123' }
    });
    expect(field('Client ID').value).toBe('');
    expect(field('Client secret').value).toBe('');
  });

  it("says OMDb's test spends one request of its daily quota", async () => {
    await show(SourceConnectorCard, {
      source: { name: 'omdb', has_api_key: true, secrets_unreadable: false, required: false, used_by: 'omdb' }
    });
    expect(target.querySelector('[data-quota]').textContent).toContain('daily quota');
    expect(button('Test').disabled).toBe(false);
  });
});
