/**
 * @vitest-environment jsdom
 *
 * §6.6 Data's three ledger editors: each ledger's own routes and form rules, and the hand-off from
 * the reject review. Spec v2.1 §6.6 Data, §8 stages 3 and 7, §6.4; decisions 173, 342 and 445.
 *
 * Named BESIDE `test_curated_editors.py` and `test_curated_api.py`, never instead of them
 * (decision 226): the server is the authority on every rule, and what this file holds is that the
 * three editors cannot reach one another's routes and that a form the server would refuse for a
 * missing field is caught before it is sent.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, get, post } from '$lib/api.js';
import LedgerEditor from './components/LedgerEditor.svelte';
import {
  COMPOSER_WARNING,
  CORRECTION_KINDS,
  DROP_EVIDENCE_WARNING,
  DROP_WARNING,
  LEDGERS,
  REPOINT_WARNING,
  VERDICT_ACTIONS,
  axisForm,
  emptyForm,
  exportHref,
  openVerdict,
  parseWeights,
  rowKey,
  rowsOf,
  validate,
  verdictDraft,
  visibleFields,
  weightText,
  withdrawPath,
  withdrawable
} from './ledgerEditors.svelte.js';

describe('three ledgers, three sets of routes', () => {
  it('gives each ledger its own path and its own export, and no two share one', () => {
    const paths = Object.values(LEDGERS).map((l) => l.path);
    expect(new Set(paths).size).toBe(3);
    expect(exportHref('adjudications')).toBe('/api/admin/curated/adjudications/export');
    expect(exportHref('corrections')).toBe('/api/admin/curated/corrections/export');
    expect(exportHref('axes', { facet: 'mood' })).toBe('/api/admin/curated/axes/mood/export');
    expect(withdrawPath('adjudications', { id: 4 })).toBe('/admin/curated/adjudications/4');
    expect(withdrawPath('corrections', { id: 4 })).toBe('/admin/curated/corrections/4');
    expect(withdrawPath('axes', { facet: 'mood' })).toBe('/admin/curated/axes/mood');
  });

  it("reads each list route's own shape and keys an axis by its facet", () => {
    expect(rowsOf('adjudications', { rows: [{ id: 1 }] })).toEqual([{ id: 1 }]);
    expect(rowsOf('axes', { facets: ['mood'], axes: [{ facet: 'mood' }] })).toEqual([{ facet: 'mood' }]);
    expect(rowsOf('corrections', null)).toEqual([]);
    expect(rowKey('axes', { facet: 'mood' })).toBe('mood');
    expect(rowKey('corrections', { id: 9 })).toBe(9);
  });

  it('lets the household withdraw its own rows and never a bundle row', () => {
    expect(withdrawable({ origin: 'household' })).toBe(true);
    expect(withdrawable({ origin: 'bundle' })).toBe(false);
  });

  it('says before a DROP that a dropped tag is gone until the title is extracted again', () => {
    expect(DROP_WARNING).toMatch(/gone until the title is extracted again/);
    expect(DROP_WARNING).toMatch(/brings nothing back/);
  });
});

describe('the verdict form', () => {
  const form = (over = {}) => ({ ...emptyForm('adjudications'), ...over });

  it('shows the title, the target and the quote only where the action and scope use them', () => {
    const names = (f) => visibleFields('adjudications', f).map((field) => field.name);
    expect(names(form({ action: 'DROP' }))).not.toContain('target');
    expect(names(form({ action: 'REPOINT' }))).toContain('target');
    expect(names(form({ action: 'DROP_EVIDENCE' }))).toContain('quote');
    expect(names(form({ scope: 'global' }))).not.toContain('title_id');
    expect(VERDICT_ACTIONS).toEqual(['DROP', 'REPOINT', 'DROP_EVIDENCE']);
  });

  it('builds the body in the corpus spelling and sends a hidden field as null', () => {
    const checked = validate(
      'adjudications',
      form({ action: 'drop', term: ' mood.dread ', title_id: '2', target: 'mood.cosy', quote: 'q' })
    );
    expect(checked).toEqual({
      ok: true,
      body: {
        scope: 'title',
        term: 'mood.dread',
        action: 'DROP',
        title_id: 2,
        target: null,
        quote: null,
        source: null,
        note: null
      }
    });
    const blanket = validate(
      'adjudications',
      form({ action: 'DROP', scope: 'global', term: 'mood.dread', title_id: '2' })
    );
    expect(blanket.ok && blanket.body.title_id).toBe(null);
  });

  it('refuses what the server would refuse for a missing field', () => {
    const check = (over) => validate('adjudications', form({ term: 'mood.dread', title_id: '2', ...over }));
    expect(check({}).ok, 'no action chosen').toBe(false);
    expect(check({ action: 'DROP', term: '' }).ok, 'no term').toBe(false);
    expect(check({ action: 'DROP', title_id: 'x' }).ok, 'a title id that is not a number').toBe(false);
    expect(check({ action: 'REPOINT' }).reason).toMatch(/REPOINT names/);
    expect(check({ action: 'DROP_EVIDENCE', scope: 'global', quote: 'q' }).ok, 'global').toBe(false);
    expect(check({ action: 'DROP_EVIDENCE' }).ok, 'no quote').toBe(false);
  });
});

describe('the correction form', () => {
  it('needs a title, a kind the derive applies, a credit and the evidence that settles it', () => {
    const value = 'Dario Marianelli';
    const ok = { title_id: '3', kind: 'composer', value, evidence: 'end credits', note: '' };
    expect(validate('corrections', ok)).toEqual({
      ok: true,
      body: { title_id: 3, kind: 'composer', value, evidence: 'end credits', note: null }
    });
    expect(validate('corrections', { ...ok, evidence: ' ' }).reason).toMatch(/evidence/);
    expect(validate('corrections', { ...ok, kind: 'director' }).ok).toBe(false);
    expect(validate('corrections', { ...ok, title_id: '' }).ok).toBe(false);
    expect(CORRECTION_KINDS).toEqual(['composer', 'composer_add']);
  });
});

describe('the axis form', () => {
  it('reads one term and one weight per line, from -1 to 1, each term once, at least one', () => {
    expect(parseWeights('mood.dread -0.8\r\n\r\nmood.cosy\t0.9')).toEqual({
      ok: true,
      weights: [
        { term: 'mood.dread', weight: -0.8 },
        { term: 'mood.cosy', weight: 0.9 }
      ]
    });
    expect(parseWeights('').ok).toBe(false);
    expect(parseWeights('mood.dread 1.5').reason).toMatch(/-1 to 1/);
    expect(parseWeights('mood.dread 0.1\nmood.dread 0.2').reason).toMatch(/twice/);
    expect(parseWeights('mood dread 0.1').ok).toBe(false);
  });

  it('needs the facet and both poles', () => {
    const ok = { facet: 'mood', left_pole: 'bleak', right_pole: 'warm', weights: 'mood.cosy 0.9' };
    expect(validate('axes', ok).ok).toBe(true);
    expect(validate('axes', { ...ok, facet: '' }).ok).toBe(false);
    expect(validate('axes', { ...ok, right_pole: '' }).ok).toBe(false);
  });

  // m56-curated-02: an axis opened for editing is the whole stored axis in the form's own lines, the
  // stored float4 printed as the shortest number that is the same float4 - so a save that changes
  // nothing writes back exactly what was there.
  it('reads a stored axis back into the form as the lines it would save', () => {
    expect(weightText(0.10000000149011612)).toBe('0.1');
    expect(weightText(-0.800000011920929)).toBe('-0.8');
    expect(weightText(1)).toBe('1');
    const row = {
      facet: 'mood',
      left_pole: 'bleak',
      right_pole: 'warm',
      origin: 'household',
      weights: [
        { term: 'mood.cosy', weight: 0.8999999761581421 },
        { term: 'mood.dread', weight: -0.800000011920929 }
      ]
    };
    const form = axisForm(row);
    expect(form).toEqual({
      facet: 'mood',
      left_pole: 'bleak',
      right_pole: 'warm',
      weights: 'mood.cosy 0.9\nmood.dread -0.8'
    });
    expect(parseWeights(form.weights).weights.map((w) => Math.fround(w.weight))).toEqual(
      row.weights.map((w) => Math.fround(w.weight))
    );
  });
});

describe('the mounted editors', () => {
  let target;

  beforeEach(() => {
    target = document.createElement('div');
    document.body.appendChild(target);
    vi.mocked(get).mockReset();
    vi.mocked(post).mockReset();
    vi.mocked(api).mockReset();
  });

  afterEach(() => {
    target.remove();
  });

  async function settle() {
    for (let i = 0; i < 40; i++) await Promise.resolve();
    flushSync();
  }

  it('offers Withdraw on the household row alone and writes only to its own ledger', async () => {
    vi.mocked(get).mockResolvedValue({
      rows: [
        { id: 1, scope: 'title', title_id: 2, name: 'Prisoners', term: 'mood.dread', action: 'DROP',
          origin: 'household' },
        { id: 2, scope: 'global', title_id: null, name: null, term: 'mood.cosy', action: 'DROP',
          origin: 'bundle' }
      ],
      applies: 'DNA verdicts apply at ingest.'
    });
    vi.mocked(api).mockResolvedValue(null);
    const app = mount(LedgerEditor, { target, props: { ledger: 'adjudications' } });
    await settle();
    try {
      expect(vi.mocked(get)).toHaveBeenCalledWith('/admin/curated/adjudications');
      const rows = [...target.querySelectorAll('li')];
      const withdraws = rows.map((li) =>
        [...li.querySelectorAll('button')].some((b) => b.textContent === 'Withdraw')
      );
      expect(withdraws).toEqual([true, false]);
      expect(target.textContent).toContain('DNA verdicts apply at ingest.');
      const href = target.querySelector('a.export').getAttribute('href');
      expect(href).toBe('/api/admin/curated/adjudications/export');
      rows[0].querySelector('button').click();
      await settle();
      expect(vi.mocked(api), 'the first tap only asks').not.toHaveBeenCalled();
      const buttons = [...rows[0].querySelectorAll('button')];
      buttons.find((b) => b.textContent.trim() === 'Yes, withdraw').click();
      await settle();
      expect(vi.mocked(api)).toHaveBeenCalledWith('/admin/curated/adjudications/1', { method: 'DELETE' });
    } finally {
      unmount(app);
    }
  });

  it('opens prefilled from the review, warns before a DROP, and posts to its own route', async () => {
    vi.mocked(get).mockResolvedValue({ rows: [], applies: '' });
    vi.mocked(post).mockResolvedValue({ row: { id: 3 }, applied: {} });
    const app = mount(LedgerEditor, { target, props: { ledger: 'adjudications' } });
    await settle();
    try {
      openVerdict({ scope: 'title', term: 'mood.cosy', title_id: '2' });
      await settle();
      expect(verdictDraft.prefill.term).toBe('mood.cosy');
      const inputs = [...target.querySelectorAll('input')];
      expect(inputs.map((i) => i.value)).toContain('mood.cosy');
      const action = target.querySelector('select');
      action.value = 'DROP';
      action.dispatchEvent(new Event('change'));
      await settle();
      expect(target.querySelector('[data-testid="verdict-warning"]').textContent).toBe(DROP_WARNING);
      target.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }));
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/admin/curated/adjudications', {
        scope: 'title',
        term: 'mood.cosy',
        action: 'DROP',
        title_id: 2,
        target: null,
        quote: null,
        source: null,
        note: null
      });
    } finally {
      unmount(app);
    }
  });

  // m56-curated-01: a `composer` correction replaces every music credit the title carries, and
  // withdrawing it restores none - on a bundle title, which has no raw store, for good. The editor
  // says so before the save and again at the second tap of Withdraw, and not for `composer_add`.
  it('warns before a composer correction is saved and again before one is withdrawn', async () => {
    vi.mocked(get).mockResolvedValue({
      rows: [
        { id: 5, title_id: 1, name: 'Grey Harbour', kind: 'composer', value: 'A Mistake',
          evidence: 'end credits', origin: 'household' },
        { id: 6, title_id: 1, name: 'Grey Harbour', kind: 'composer_add', value: 'An Extra',
          evidence: 'end credits', origin: 'household' }
      ],
      applies: ''
    });
    vi.mocked(api).mockResolvedValue(null);
    const app = mount(LedgerEditor, { target, props: { ledger: 'corrections' } });
    await settle();
    try {
      expect(COMPOSER_WARNING).toMatch(/replaces every music credit/);
      expect(COMPOSER_WARNING).toMatch(/gone for good/);
      const warned = () => target.querySelectorAll('[data-testid="composer-warning"]').length;
      expect(warned(), 'nothing is said before anything is asked').toBe(0);

      const rows = [...target.querySelectorAll('li')];
      rows[1].querySelector('button').click();
      await settle();
      expect(warned(), 'composer_add replaces nothing').toBe(0);
      [...rows[1].querySelectorAll('button')].find((b) => b.textContent.trim() === 'Keep it').click();
      await settle();
      rows[0].querySelector('button').click();
      await settle();
      expect(rows[0].querySelector('[data-testid="composer-warning"]').textContent).toBe(
        COMPOSER_WARNING
      );
      expect(vi.mocked(api), 'the warning is shown before the write, not after').not.toHaveBeenCalled();
      [...rows[0].querySelectorAll('button')].find((b) => b.textContent.trim() === 'Keep it').click();
      await settle();

      [...target.querySelectorAll('button')].find((b) => b.textContent === 'Add a correction').click();
      await settle();
      const kind = target.querySelector('form select');
      expect(kind.value).toBe('composer');
      expect(target.querySelector('form [data-testid="composer-warning"]').textContent).toBe(
        COMPOSER_WARNING
      );
      kind.value = 'composer_add';
      kind.dispatchEvent(new Event('change'));
      await settle();
      expect(warned()).toBe(0);
    } finally {
      unmount(app);
    }
  });

  it("prints the server's sentence on what reads an axis and exports a household axis", async () => {
    vi.mocked(get).mockResolvedValue({
      version: 'v1',
      facets: ['mood', 'era'],
      axes: [{ facet: 'mood', left_pole: 'bleak', right_pole: 'warm', origin: 'household', weights: [] }],
      applies: 'An axis is read live. No axis file has been authored and the bundle ships none.'
    });
    const app = mount(LedgerEditor, { target, props: { ledger: 'axes' } });
    await settle();
    try {
      expect(target.querySelector('[data-testid="ledger-applies"]').textContent).toBe(
        'An axis is read live. No axis file has been authored and the bundle ships none.'
      );
      const hrefs = [...target.querySelectorAll('a.export')].map((a) => a.getAttribute('href'));
      expect(hrefs).toEqual(['/api/admin/curated/axes/mood/export']);
    } finally {
      unmount(app);
    }
  });

  // m56-curated-03: DROP_EVIDENCE matches its quote as a case-insensitive substring and drops a tag
  // left with no quote, and a REPOINT merges into a tag already there - neither is undone by
  // withdrawing the verdict (decision 445), so each says so before the save, as DROP does.
  it('warns before a DROP_EVIDENCE and a REPOINT as it does before a DROP', async () => {
    vi.mocked(get).mockResolvedValue({ rows: [], applies: '' });
    const app = mount(LedgerEditor, { target, props: { ledger: 'adjudications' } });
    await settle();
    try {
      [...target.querySelectorAll('button')].find((b) => b.textContent === 'Add a verdict').click();
      await settle();
      const warning = () => target.querySelector('[data-testid="verdict-warning"]')?.textContent ?? null;
      expect(warning(), 'nothing is said before an action is chosen').toBe(null);
      const action = target.querySelector('form select');
      const choose = async (value) => {
        action.value = value;
        action.dispatchEvent(new Event('change'));
        await settle();
      };
      await choose('DROP_EVIDENCE');
      expect(warning()).toBe(DROP_EVIDENCE_WARNING);
      expect(DROP_EVIDENCE_WARNING).toMatch(/case-insensitive/);
      expect(DROP_EVIDENCE_WARNING).toMatch(/no quote left is dropped/);
      await choose('REPOINT');
      expect(warning()).toBe(REPOINT_WARNING);
      expect(REPOINT_WARNING).toMatch(/moves nothing back/);
      await choose('DROP');
      expect(warning()).toBe(DROP_WARNING);
    } finally {
      unmount(app);
    }
  });

  // M56-DATA-06: the hand-off is module state and outlives the page, so a verdict editor mounted
  // again - the operator back on /admin/data from another admin tab - must not reopen, and scroll
  // to, a form they already dismissed. A new tap on the review still opens it.
  it('does not reopen a dismissed hand-off when the editor mounts again, but opens a new one', async () => {
    vi.mocked(get).mockResolvedValue({ rows: [], applies: '' });
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    try {
      const first = mount(LedgerEditor, { target, props: { ledger: 'adjudications' } });
      await settle();
      openVerdict({ scope: 'title', term: 'mood.dread', title_id: '2' });
      await settle();
      expect(target.querySelector('form'), 'the hand-off opens the form').not.toBe(null);
      [...target.querySelectorAll('button')].find((b) => b.textContent === 'Cancel').click();
      await settle();
      expect(target.querySelector('form')).toBe(null);
      unmount(first);

      scrolled.mockClear();
      const again = mount(LedgerEditor, { target, props: { ledger: 'adjudications' } });
      await settle();
      try {
        expect(target.querySelector('form'), 'a dismissed hand-off stays dismissed').toBe(null);
        expect(scrolled, 'and the page is not scrolled to it').not.toHaveBeenCalled();
        openVerdict({ scope: 'title', term: 'mood.cosy', title_id: '3' });
        await settle();
        const inputs = [...target.querySelectorAll('form input')].map((i) => i.value);
        expect(inputs).toContain('mood.cosy');
      } finally {
        unmount(again);
      }
    } finally {
      delete Element.prototype.scrollIntoView;
    }
  });

  // m56-curated-02: a save replaces the facet's whole axis (decision 261's rule, carried to the
  // editor), so Add offers only a facet with no axis, and a household axis is changed from its own
  // row, prefilled with every term it has - the save then writes what is on screen and nothing less.
  it('adds an axis only where there is none and edits a household axis prefilled whole', async () => {
    vi.mocked(get).mockResolvedValue({
      version: 'v1',
      facets: ['mood', 'era', 'pacing'],
      axes: [
        {
          facet: 'mood',
          left_pole: 'bleak',
          right_pole: 'warm',
          origin: 'household',
          weights: [
            { term: 'mood.cosy', weight: 0.8999999761581421 },
            { term: 'mood.dread', weight: -0.800000011920929 }
          ]
        },
        {
          facet: 'era',
          left_pole: 'old',
          right_pole: 'new',
          origin: 'bundle',
          weights: [{ term: 'era.silent', weight: -1 }]
        }
      ],
      applies: ''
    });
    vi.mocked(post).mockResolvedValue({ axis: {} });
    const app = mount(LedgerEditor, { target, props: { ledger: 'axes' } });
    await settle();
    try {
      const button = (within, label) =>
        [...within.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
      const choices = () =>
        [...target.querySelectorAll('form select option')].map((o) => o.value).filter((v) => v !== '');
      button(target, 'Add an axis').click();
      await settle();
      expect(choices(), 'a facet that has an axis is not offered to Add').toEqual(['pacing']);
      button(target, 'Cancel').click();
      await settle();

      const rows = [...target.querySelectorAll('li')];
      expect(
        rows.map((li) => Boolean(button(li, 'Edit'))),
        'the household axis is edited from its row; the bundle one is read-only'
      ).toEqual([true, false]);
      button(rows[0], 'Edit').click();
      await settle();
      expect(choices()).toEqual(['mood']);
      expect(target.querySelector('form select').value).toBe('mood');
      expect([...target.querySelectorAll('form input')].map((i) => i.value)).toEqual(['bleak', 'warm']);
      expect(target.querySelector('form textarea').value).toBe('mood.cosy 0.9\nmood.dread -0.8');
      expect(target.querySelector('[data-testid="axis-replace-note"]')).not.toBe(null);
      expect(target.querySelector('form button[type="submit"]').textContent.trim()).toBe('Replace axis');
      target.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }));
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/admin/curated/axes', {
        facet: 'mood',
        left_pole: 'bleak',
        right_pole: 'warm',
        weights: [
          { term: 'mood.cosy', weight: 0.9 },
          { term: 'mood.dread', weight: -0.8 }
        ]
      });
    } finally {
      unmount(app);
    }
  });
});
