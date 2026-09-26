import { describe, expect, it } from 'vitest';

import { termLabel } from './terms.js';

// Dotted ids on purpose: the backend's fixture vocabulary is dotless.

describe('termLabel (§6.8 vocabulary terms)', () => {
  it('renders the label the payload carries', () => {
    expect(termLabel({ term: 'era.wwii', label: 'World War II' })).toBe('World War II');
    expect(termLabel({ term: 'themes.love_romance', label: ' love & romance ' })).toBe('love & romance');
  });

  it('falls back to the leaf in plain words when there is no label', () => {
    expect(termLabel({ term: 'characters.morally_grey', label: null })).toBe('morally grey');
    expect(termLabel({ term: 'pacing.relentless', label: '  ' })).toBe('relentless');
    expect(termLabel({ term: 'era.renaissance_early_modern' })).toBe('renaissance early modern');
  });

  it('accepts a bare id and never returns one', () => {
    expect(termLabel('pacing.relentless')).toBe('relentless');
    expect(termLabel('sound.score_forward')).toBe('score forward');
    expect(termLabel('obsession')).toBe('obsession');
    for (const id of ['era.wwii', 'characters.friendship_bond', 'mood.cosy']) {
      expect(termLabel(id)).not.toMatch(/[._]/);
    }
  });

  it('answers an empty string for nothing', () => {
    expect(termLabel(null)).toBe('');
    expect(termLabel(undefined)).toBe('');
    expect(termLabel({})).toBe('');
  });
});
