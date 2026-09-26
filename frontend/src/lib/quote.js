// Ellipses mark a verbatim span cut mid-sentence, which printed bare can read as its opposite;
// the stored quote, which stage 7 verified, is never altered.

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
