import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, get, post } from '$lib/api.js';
import {
  amend,
  basisLine,
  cancel,
  confirm,
  invalidate,
  propose,
  saveCap,
  saveKey,
  spend,
  usd
} from './spendGuard.svelte.js';

/**
 * The spend guard's sequencing, counted in requests. Spec v2.1 §6.6; decisions 450, 452; plan §7
 * checks 1-3.
 *
 * §6.6's "per-title cost estimate before enabling" is a claim about ORDER, and order is exactly
 * what a screenshot cannot show: a card that saved optimistically and then printed the figure
 * looks identical to one that asked first. So each rule here is asserted as the requests the
 * module made, against a mocked wire -- the server holds the same order on its side
 * (`test_spend_guard.py`), and this is the half that decides whether a Confirm under the thumb can
 * reach `PUT /admin/llm` without a figure on screen for exactly the change it stores.
 */

/** `POST /admin/llm/preview`'s answer, as `api/llm._preview_body` spells it. */
const preview = (perTitle = '0.032175', over = {}) => ({
  estimate: {
    per_title_usd: perTitle,
    input_tokens_assumed: 23500,
    output_tokens_assumed: 3900,
    passes: 1,
    providers: ['gemini'],
    reason: null,
    basis: [
      {
        provider: 'gemini',
        model: 'gemini-3.7-flash',
        source: 'table',
        input: 0.75,
        output: 3.75,
        valid_until: '2027-01-01',
        then: { input: 1.5, output: 7.5 }
      }
    ]
  },
  projected: {
    window_days: 30,
    titles: 12,
    ever_filed: true,
    monthly_usd: '0.3861',
    remaining_usd: '20.88',
    exceeds_remaining: false,
    reason: null
  },
  meter: { spent_usd: '4.12', cap_usd: '25', remaining_usd: '20.88' },
  blocked: null,
  ...over
});

/** A promise the test settles by hand, for a preview still on the wire. */
function deferred() {
  let resolve = /** @type {(value: any) => void} */ (() => {});
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(get).mockResolvedValue({});
  // Module state is one object for the run, which is the point of it; each case starts from no
  // proposal rather than from its predecessor's.
  cancel();
  spend.llm = null;
});

describe('the number comes before the setting (decision 450)', () => {
  it('a confirm with no preview on screen sends nothing', async () => {
    expect(await confirm()).toBe(false);
    expect(api).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it('a proposal asks the preview and writes nothing', async () => {
    vi.mocked(post).mockResolvedValue(preview());
    await propose({ extraction_provider: 'gemini' });
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith('/admin/llm/preview', { extraction_provider: 'gemini' });
    expect(api, 'a preview is a POST to the preview route and nothing else').not.toHaveBeenCalled();
    expect(spend.pending.preview.estimate.per_title_usd).toBe('0.032175');
  });

  it('a confirm carries exactly the change previewed and the figure the preview showed', async () => {
    vi.mocked(post).mockResolvedValue(preview('0.032175'));
    vi.mocked(api).mockResolvedValue({ providers: [], settings: {} });
    await propose({ extraction_provider: 'gemini', passes: 2 });
    expect(await confirm()).toBe(true);
    expect(api).toHaveBeenCalledTimes(1);
    expect(api).toHaveBeenCalledWith('/admin/llm', {
      method: 'PUT',
      body: { extraction_provider: 'gemini', passes: 2, accepted_estimate: '0.032175' }
    });
    expect(spend.proposal, 'a stored change is no longer a proposal').toBeNull();
    expect(spend.pending).toBeNull();
  });

  it('an edit after the preview makes the old figure unconfirmable at once', async () => {
    vi.mocked(post).mockResolvedValueOnce(preview('0.032175'));
    await propose({ extraction_provider: 'gemini' });
    // The next figure is still on the wire: nothing on screen is for the change as it stands.
    const next = deferred();
    vi.mocked(post).mockReturnValueOnce(next.promise);
    const asked = propose({ extraction_provider: 'gemini', passes: 3 });
    expect(spend.pending, 'the old figure went before the request did').toBeNull();
    expect(await confirm()).toBe(false);
    expect(api).not.toHaveBeenCalled();
    next.resolve(preview('0.096525'));
    await asked;
    vi.mocked(api).mockResolvedValue({});
    await confirm();
    expect(api).toHaveBeenCalledWith('/admin/llm', {
      method: 'PUT',
      body: { extraction_provider: 'gemini', passes: 3, accepted_estimate: '0.096525' }
    });
  });

  it('a typed but uncommitted edit invalidates the figure with no request', async () => {
    vi.mocked(post).mockResolvedValue(preview());
    await propose({ providers: { gemini: { model: 'gemini-3.7-flash' } } });
    vi.mocked(post).mockClear();
    invalidate();
    expect(post).not.toHaveBeenCalled();
    expect(await confirm()).toBe(false);
    expect(api).not.toHaveBeenCalled();
  });

  it('a preview that answers after a later edit is discarded, not shown for the wrong change', async () => {
    const early = deferred();
    const late = deferred();
    vi.mocked(post).mockReturnValueOnce(early.promise).mockReturnValueOnce(late.promise);
    const first = propose({ passes: 1 });
    const second = propose({ passes: 2 });
    late.resolve(preview('0.06435'));
    await second;
    early.resolve(preview('0.032175'));
    await first;
    expect(spend.pending.change).toEqual({ passes: 2 });
    expect(spend.pending.preview.estimate.per_title_usd).toBe('0.06435');
  });

  it('cancel makes no request at all', async () => {
    vi.mocked(post).mockResolvedValue(preview());
    await propose({ extraction_provider: 'gemini' });
    vi.mocked(post).mockClear();
    vi.mocked(get).mockClear();
    cancel();
    expect(post).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
    expect(api).not.toHaveBeenCalled();
    expect(spend.proposal).toBeNull();
    expect(spend.pending).toBeNull();
    expect(await confirm(), 'and there is nothing left to confirm').toBe(false);
  });

  it('a 409 replaces the pending figure with a fresh one and stores nothing', async () => {
    vi.mocked(post)
      .mockResolvedValueOnce(preview('0.032175'))
      .mockResolvedValueOnce(preview('0.06435'));
    const refusal = Object.assign(new Error('the per-title estimate is now 0.06435'), { status: 409 });
    vi.mocked(api).mockRejectedValueOnce(refusal);
    await propose({ extraction_provider: 'gemini' });
    expect(await confirm()).toBe(false);
    expect(api, 'one write, refused; never a second one on the client side').toHaveBeenCalledTimes(1);
    // The same change, asked again: `api.js` hands on the refusal's detail and not its preview.
    expect(post).toHaveBeenLastCalledWith('/admin/llm/preview', { extraction_provider: 'gemini' });
    expect(spend.pending.preview.estimate.per_title_usd).toBe('0.06435');
    expect(spend.refused).toContain('0.06435');
    vi.mocked(api).mockResolvedValue({});
    await confirm();
    expect(api).toHaveBeenLastCalledWith('/admin/llm', {
      method: 'PUT',
      body: { extraction_provider: 'gemini', accepted_estimate: '0.06435' }
    });
  });

  it('a preview that names a blocked provider cannot be confirmed', async () => {
    vi.mocked(post).mockResolvedValue(
      preview('0.032175', { blocked: 'no API key is configured for gemini' })
    );
    await propose({ extraction_provider: 'gemini' });
    expect(await confirm()).toBe(false);
    expect(api).not.toHaveBeenCalled();
  });

  it('amending the only edited field back out is the same as cancel', async () => {
    vi.mocked(post).mockResolvedValue(preview());
    await propose({ passes: 2 });
    vi.mocked(post).mockClear();
    await propose(amend(spend.proposal, { passes: undefined }));
    expect(post, 'an empty change previews nothing').not.toHaveBeenCalled();
    expect(spend.proposal).toBeNull();
  });
});

describe('the proposal grammar', () => {
  it('merges providers per field, keeps null as unset and drops undefined', () => {
    const base = { extraction_provider: 'gemini', providers: { gemini: { model: 'a' } } };
    expect(amend(base, { providers: { gemini: { price_input: 1, price_output: 2 } } })).toEqual({
      extraction_provider: 'gemini',
      providers: { gemini: { model: 'a', price_input: 1, price_output: 2 } }
    });
    expect(amend(base, { extraction_provider: null })).toEqual({
      extraction_provider: null,
      providers: { gemini: { model: 'a' } }
    });
    expect(amend(base, { providers: { gemini: { model: undefined } } })).toEqual({
      extraction_provider: 'gemini'
    });
  });
});

describe('the cap and the keys (decision 452)', () => {
  it('refuses a cap that is not a finite number of at least zero without asking', async () => {
    for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY, '25', null, true]) {
      expect(await saveCap(/** @type {any} */ (bad))).toBe(false);
    }
    expect(api).not.toHaveBeenCalled();
  });

  it('writes a cap of zero, which is a cap meaning spend nothing', async () => {
    vi.mocked(api).mockResolvedValue({ meter: { cap_usd: '0' } });
    expect(await saveCap(0)).toBe(true);
    expect(api).toHaveBeenCalledWith('/admin/llm/cap', { method: 'PUT', body: { cap_usd: 0 } });
  });

  it('empties the key field it was given, whether the save landed or not', async () => {
    const fields = { api_key: 'sk-live-not-a-real-key' };
    vi.mocked(api).mockResolvedValue({ name: 'gemini', has_api_key: true });
    expect((await saveKey('gemini', fields)).ok).toBe(true);
    expect(api).toHaveBeenCalledWith('/admin/connectors/gemini', {
      method: 'PUT',
      body: { api_key: 'sk-live-not-a-real-key' }
    });
    expect(fields.api_key).toBe('');

    const refused = { api_key: 'sk-typo' };
    vi.mocked(api).mockRejectedValueOnce(new Error('api_key: String should have at most 512'));
    expect((await saveKey('gemini', refused)).ok).toBe(false);
    expect(refused.api_key, 'a refused save leaves no key in the form').toBe('');
  });

  it('sends only the fields that hold text, and nothing when none does', async () => {
    vi.mocked(api).mockResolvedValue({});
    await saveKey('trakt', { client_id: '', client_secret: 'shh' });
    expect(api).toHaveBeenCalledWith('/admin/connectors/trakt', {
      method: 'PUT',
      body: { client_secret: 'shh' }
    });
    vi.mocked(api).mockClear();
    await saveKey('tmdb', { api_key: '' });
    expect(api, 'an empty field keeps the stored key, so there is nothing to send').not.toHaveBeenCalled();
  });

  it('sends a key trimmed, and nothing for a field of whitespace (M57-KEYS-C1-02)', async () => {
    vi.mocked(api).mockResolvedValue({});
    await saveKey('tmdb', { api_key: '  d41d8cd98f00b204e9800998ecf8427e \t' });
    expect(api).toHaveBeenCalledWith('/admin/connectors/tmdb', {
      method: 'PUT',
      body: { api_key: 'd41d8cd98f00b204e9800998ecf8427e' }
    });
    vi.mocked(api).mockClear();
    const blank = { api_key: '   ' };
    expect((await saveKey('tmdb', blank)).ok).toBe(false);
    expect(api, 'a field of spaces keeps the stored key, so there is nothing to send').not.toHaveBeenCalled();
    expect(blank.api_key).toBe('');
  });
});

describe('the figures as the cards print them', () => {
  it('keeps every digit the server sent and at least the cents', () => {
    expect(usd('0.03217500')).toBe('$0.032175');
    expect(usd('25')).toBe('$25.00');
    expect(usd('4.1200')).toBe('$4.12');
    expect(usd(0.75)).toBe('$0.75');
    expect(usd('unknown')).toBe('unknown');
    expect(usd(null)).toBeNull();
  });

  it('names the model, the rates, their source and the date the price changes (decision 343)', () => {
    const line = basisLine(preview().estimate.basis[0]);
    expect(line).toContain('gemini-3.7-flash');
    expect(line).toContain('$0.75 in / $3.75 out per 1M tokens');
    expect(line).toContain('shipped table');
    expect(line).toContain('valid until 2027-01-01, then $1.50 / $7.50');
    expect(basisLine({ ...preview().estimate.basis[0], source: 'override', valid_until: null })).toContain(
      'admin override'
    );
    expect(basisLine('unknown')).toBe('price unknown');
  });
});
