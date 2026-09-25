import { describe, expect, it } from 'vitest';

import { quoteText } from './quote.js';

/** §4.1 rule 1 and §6.0: a quote cut from something longer says so, and says nothing else. */

describe('quoteText (evidence quotes as fragments)', () => {
  it('marks a span that starts mid-sentence and stops short of its end', () => {
    expect(quoteText("not this serious, gritty crime epic that's being attempted here")).toBe(
      "…not this serious, gritty crime epic that's being attempted here…"
    );
  });

  it('leaves a whole sentence exactly as stored', () => {
    expect(quoteText('The score is arguing with the picture.')).toBe(
      'The score is arguing with the picture.'
    );
    expect(quoteText('Who is watching whom?"')).toBe('Who is watching whom?"');
  });

  it('marks only the end it is missing', () => {
    expect(quoteText('Lit entirely by signage and rain')).toBe('Lit entirely by signage and rain…');
    expect(quoteText('a low hum of dread that outlasts the final scene.')).toBe(
      '…a low hum of dread that outlasts the final scene.'
    );
  });

  it('changes no character of the stored quote', () => {
    const stored = 'everyone is being watched';
    expect(quoteText(stored).replaceAll('…', '')).toBe(stored);
    expect(quoteText('')).toBe('');
    expect(quoteText(null)).toBe('');
    // A span that opens on a digit, or on a quotation mark before a capital, is not a lower-case
    // start.
    expect(quoteText('1959, and a hearing.')).toBe('1959, and a hearing.');
  });
});
