/**
 * @vitest-environment jsdom
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
  emptyForm,
  exportHref,
  openVerdict,
  validate,
  verdictDraft,
  visibleFields,
  withdrawPath,
  withdrawable
} from './ledgerEditors.svelte.js';

describe('two ledgers, two sets of routes', () => {
  it('gives each ledger its own path and its own export, and no two share one', () => {
    const paths = Object.values(LEDGERS).map((l) => l.path);
    expect(new Set(paths).size).toBe(2);
    expect(exportHref('adjudications')).toBe('/api/admin/curated/adjudications/export');
    expect(exportHref('corrections')).toBe('/api/admin/curated/corrections/export');
    expect(withdrawPath('adjudications', { id: 4 })).toBe('/admin/curated/adjudications/4');
    expect(withdrawPath('corrections', { id: 4 })).toBe('/admin/curated/corrections/4');
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

  // The hand-off is module state and outlives the page.
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
});
