import { describe, expect, it } from 'vitest';

import {
  ANSWERS,
  BUDGET_DEFAULT,
  BUDGET_MAX,
  BUDGET_MIN,
  BUDGET_STEP,
  ESCAPE_LABEL,
  JOIN_CAPTION,
  MAX_GUESTS,
  REVEAL_BEAT,
  approvalShare,
  minutesAgo,
  progressLine,
  roomLine
} from './tonight.svelte.js';

/**
 * The Tonight client's pure helpers. Spec v2.1 §6.2 (rewritten), §6.8.
 *
 * Three of these render a spec sentence, so the test is what keeps the sentence from drifting:
 * §6.2 step 2's open-rooms row, 54c's waiting line, and §6.8's data voice on the approval share.
 * The fourth — the four answers — is decision 154's whole content.
 */

describe('the open-rooms row (§6.2 step 2)', () => {
  const room = {
    room_code: 'MX-2210',
    host: 'Mia',
    started_at: new Date(Date.now() - 3 * 60000).toISOString(),
    kind: 'movie',
    runtime_budget_min: 60,
    skips_seen: true
  };

  it('reads the way the spec writes it', () => {
    // §6.2's own example: "MX-2210 · hosted by Mia · 3 min ago · Film · 60 min · skips seen".
    expect(roomLine(room)).toBe('MX-2210 · hosted by Mia · 3 min ago · Film · 60 min · skips seen');
  });

  it('says the opposite when rewatches are in', () => {
    expect(roomLine({ ...room, skips_seen: false })).toContain('includes rewatches');
  });

  it('names the kind the way the controls do', () => {
    expect(roomLine({ ...room, kind: 'series' })).toContain('Series');
  });

  it('drops the age rather than printing a lie when the timestamp is missing', () => {
    // A row is still a row without an age; "NaN min ago" is worse than one fewer facet.
    expect(roomLine({ ...room, started_at: null })).not.toContain('min ago');
    expect(roomLine({ ...room, started_at: null })).toContain('MX-2210');
    expect(minutesAgo('not-a-date')).toBeNull();
    expect(minutesAgo(null)).toBeNull();
  });

  it('never reports a negative age from a clock that is slightly ahead', () => {
    const ahead = new Date(Date.now() + 30000).toISOString();
    expect(minutesAgo(ahead)).toBe(0);
  });
});

describe('the waiting line (54c)', () => {
  const progress = [
    { name: 'Patrick', answered: 6, expected: 20, finished: true },
    { name: 'Jenny', answered: 9, expected: 20, finished: false },
    { name: 'Mia', answered: 4, expected: 20, finished: false }
  ];

  it('shows counts and names, and nothing that could be an answer', () => {
    // 54c: "progress and never their answers". The payload cannot carry them; this is the
    // second half — the renderer has nothing to draw them from either.
    const line = progressLine(progress);
    expect(line).toContain('Patrick 6/6 done');
    expect(line).toContain('Jenny 9/~20');
    expect(line).toContain('waiting for 2');
    expect(line).not.toMatch(/EITHER|NEITHER|title/i);
  });

  it('stops saying "waiting" once everybody has finished', () => {
    const done = progress.map((p) => ({ ...p, finished: true }));
    expect(progressLine(done)).not.toContain('waiting for');
  });

  it('is empty rather than wrong with nobody seated', () => {
    expect(progressLine([])).toBe('');
  });
});

describe('the approval share (§6.8, §13)', () => {
  it('is a count next to its name, never a bare number', () => {
    // §6.8: model numbers appear in the data voice next to their name, never bare. §13 makes
    // this the headline metric for the whole feature.
    expect(approvalShare({ approval_share: 0.75, participants: 4 })).toBe('3 of 4 approved');
    expect(approvalShare({ approval_share: 1, participants: 2 })).toBe('2 of 2 approved');
    expect(approvalShare({ approval_share: 0, participants: 3 })).toBe('0 of 3 approved');
  });

  it('says nothing at all before there is a result', () => {
    expect(approvalShare(null)).toBe('');
  });
});

describe('the constants the spec fixes', () => {
  it('offers exactly decision 154\'s four answers', () => {
    expect(ANSWERS.map((a) => a.value)).toEqual(['A', 'B', 'EITHER', 'NEITHER']);
    // The two level answers are opposite signals, so their copy has to be opposite too — the
    // prototype's "Neither pulls me tonight" is §6.2's own string.
    expect(ANSWERS.find((a) => a.value === 'EITHER').label).toBe('Either is fine');
    expect(ANSWERS.find((a) => a.value === 'NEITHER').label).toBe('Neither pulls me tonight');
  });

  it('bounds the runtime slider', () => {
    expect([BUDGET_MIN, BUDGET_MAX, BUDGET_STEP, BUDGET_DEFAULT]).toEqual([60, 200, 5, 130]);
    expect(BUDGET_DEFAULT % BUDGET_STEP).toBe(0);
  });

  it('caps the guests who share one phone', () => {
    expect(MAX_GUESTS).toBe(6);
  });

  it('keeps the two strings the spec fixes verbatim', () => {
    expect(REVEAL_BEAT).toBe('VOTES REVEALED TOGETHER');
    expect(ESCAPE_LABEL).toBe('just pick for us');
    expect(JOIN_CAPTION).toContain('Push is best effort');
  });
});
