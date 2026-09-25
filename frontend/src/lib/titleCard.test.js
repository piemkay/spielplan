/**
 * The title card's sentences, one rule at a time. Spec v2.1 §6.0, §6.8, §7.3; decisions 486, 487.
 *
 * Each case pins a string a member read in the 2026-09-25 user test, or the rule that replaced it.
 * `TitleDetail.svelte.test.js` mounts the card over payloads; this file holds the rules' edges.
 */

import { describe, expect, it } from 'vitest';

import {
  answeredLine,
  creditJobs,
  creditKey,
  playWhy,
  quoteText,
  revealLine,
  sourceLabel,
  syncNote
} from './titleCard.js';

/** Section signs, decision/proposal numbers and milestone labels: decision 486 clause 2. */
const REFERENCE = /§\s?\d|decision \d|proposal \d|\bM[0-7](\.\d+)?\b/i;

describe('syncNote', () => {
  it('says Jellyfin was told only when it was', () => {
    expect(syncNote({ synced: true, reason: null })).toBe('Saved, and Jellyfin is up to date.');
    expect(syncNote({ synced: false, reason: null })).toBe('Saved here - Jellyfin was not told.');
  });

  it("lets decision 210(a)'s reason win over a success", () => {
    const note = syncNote({ synced: true, reason: 'series unseen is app-only' });
    expect(note).toContain('Saved here only');
    expect(note).not.toContain('up to date');
  });

  it('maps every reason the seen sync publishes to a plain sentence', () => {
    const reasons = [
      'not on Jellyfin',
      'no per-user Jellyfin token — re-link required',
      'Jellyfin rejected the per-user token — re-link required',
      'this account is not linked to a Jellyfin user',
      'Jellyfin not configured',
      'connector secrets unreadable (SECRETS_KEY)',
      'another write for this title is in flight',
      'DELETE /UserPlayedItems/jf-1 -> 404'
    ];
    for (const reason of reasons) {
      const note = syncNote({ synced: false, reason });
      expect(note, reason).toMatch(/^Saved/);
      // None of the rail's own words reaches the card.
      expect(note, reason).not.toMatch(/token|SECRETS_KEY|per-user|UserPlayedItems|in flight/);
    }
  });
});

describe('playWhy', () => {
  it('tells a title outside the library from a household with no server', () => {
    expect(playWhy('not_in_library')).toBe('Not in your Jellyfin library.');
    expect(playWhy('no_server')).toContain("Jellyfin isn't connected");
    expect(playWhy(null)).toBe('');
    for (const reason of ['not_in_library', 'no_server']) {
      expect(playWhy(reason)).not.toMatch(REFERENCE);
    }
  });
});

describe('quoteText', () => {
  it('marks a span that starts mid-sentence, at the start', () => {
    expect(quoteText('not this serious, gritty crime epic.')).toBe(
      '…not this serious, gritty crime epic.'
    );
  });

  it('marks a span that stops mid-sentence, at the end', () => {
    expect(quoteText('The work eats the man and he lets it')).toBe(
      'The work eats the man and he lets it…'
    );
  });

  it('leaves a whole sentence alone, closing quote and all', () => {
    expect(quoteText('A war film.')).toBe('A war film.');
    expect(quoteText('He said "go."')).toBe('He said "go."');
    expect(quoteText('Why?')).toBe('Why?');
  });

  it('reads a lower-case start behind an opening quote mark', () => {
    expect(quoteText('“and then it ends.')).toBe('…“and then it ends.');
  });

  it('says nothing for nothing', () => {
    expect(quoteText('')).toBe('');
    expect(quoteText(null)).toBe('');
  });
});

describe('sourceLabel', () => {
  it('names the platform and drops the pack index, which means nothing to a reader', () => {
    expect(sourceLabel('metacritic:3')).toBe('Metacritic');
    expect(sourceLabel('plot:1')).toBe('Plot summary');
    expect(sourceLabel('rtcritic:18')).toBe('Rotten Tomatoes critic');
    expect(sourceLabel('imdb:2')).toBe('IMDb');
  });

  it('keeps a section, which does', () => {
    expect(sourceLabel('wiki:music')).toBe('Wikipedia · music');
    expect(sourceLabel('trakt:comment')).toBe('Trakt · comment');
  });

  it('spells an unknown platform in words rather than failing', () => {
    expect(sourceLabel('letterboxd_review:2')).toBe('letterboxd review');
    expect(sourceLabel('')).toBe('');
  });
});

describe('creditJobs and creditKey', () => {
  it('prints one spelling of one credit', () => {
    expect(
      creditJobs({
        job: 'Original Music Composer',
        role_class: 'composer',
        jobs: ['Composer', 'Music', 'Original Music Composer']
      })
    ).toBe('Original Music Composer');
    expect(creditJobs({ job: 'Writer', role_class: 'writer', jobs: ['Screenplay', 'Writer'] })).toBe(
      'Writer'
    );
  });

  it('prints a different credit beside the first', () => {
    expect(
      creditJobs({ job: 'Writer', role_class: 'writer', jobs: ['Novel', 'Screenplay', 'Writer'] })
    ).toBe('Writer · Novel');
    expect(
      creditJobs({ job: 'Director', role_class: 'director', jobs: ['Co-Director', 'Director'] })
    ).toBe('Director · Co-Director');
  });

  it('is the job alone for a payload without a role class', () => {
    expect(creditJobs({ job: 'Director' })).toBe('Director');
  });

  it('keys on person and role class, and falls back to the job', () => {
    expect(creditKey({ person_id: 7, role_class: 'writer', job: 'Screenplay' })).toBe('7:writer');
    expect(creditKey({ person_id: 7, job: 'Director' })).toBe('7:Director');
    // Delimited: person 70 + class `1x` is not person 701 + class `x`.
    expect(creditKey({ person_id: 70, role_class: '1x' })).not.toBe(
      creditKey({ person_id: 701, role_class: 'x' })
    );
  });
});

describe('revealLine and answeredLine', () => {
  it("builds §6.1's phrase from the class and never prints the number", () => {
    expect(revealLine({ available: true, agreed: true, predicted_label: 'liked', cdf: 0.7 })).toBe(
      "We'd have guessed the same."
    );
    const miss = revealLine({ available: true, agreed: false, predicted_label: 'fine', cdf: 0.4 });
    expect(miss).toBe("We'd have guessed fine.");
    expect(revealLine({ available: false })).toBe('');
    expect(revealLine(null)).toBe('');
  });

  it('says what was saved in words', () => {
    expect(answeredLine('liked')).toBe('Saved - you liked it.');
    expect(answeredLine('fine')).toBe('Saved - you thought it was fine.');
    expect(answeredLine('disliked')).toBe('Saved - you disliked it.');
    expect(answeredLine('not_seen')).toBe('Saved - marked not seen.');
  });
});
