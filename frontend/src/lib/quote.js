/**
 * An evidence quote as a reader should meet it: a fragment is marked as one. Spec v2.1 §4.1 rule 1
 * ("dna_evidence ships with the extracted tier - a tag without its quote is unfalsifiable") and
 * §6.0 ("the DNA card - tags with evidence quotes").
 *
 * The extractor quotes a verbatim span, and a span can start mid-sentence: Heat's `mood.gritty`
 * rests on "not this serious, gritty crime epic that's being attempted here", from a review that
 * calls Heat exactly that, and printed bare it reads as the reviewer denying it. A leading ellipsis
 * when the span starts in lower case, and a trailing one when it stops short of a sentence's end,
 * say "cut from something longer" without changing a character of what was stored - the stored
 * quote is what stage 7 verified and what an adjudication names. [C9.6 of the 2026-09-25 user test]
 *
 * A lower-case start behind an opening quote mark is still a lower-case start. This is the one
 * copy of the rule - `titleCard.js` re-exports it - because two spellings of one rule is how one
 * of them stops matching the other.
 */

const ELLIPSIS = '…';
const LOWER_START = /^["'“‘(]*\p{Ll}/u;
const SENTENCE_END = /[.!?…]["'”’)\]]*$/;

/**
 * @param {string | null | undefined} quote
 * @returns {string}
 */
export function quoteText(quote) {
  const text = (quote ?? '').trim();
  if (!text) return '';
  const opens = LOWER_START.test(text) ? ELLIPSIS : '';
  const closes = SENTENCE_END.test(text) ? '' : ELLIPSIS;
  return opens + text + closes;
}
