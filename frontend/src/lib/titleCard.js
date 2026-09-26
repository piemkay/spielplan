/**
 * The title card's wording, as pure functions. Spec v2.1 §6.0, §6.8, §7.3; decisions 486, 487, 516,
 * 517.
 *
 * Out of `TitleDetail.svelte` because every one of these is a sentence a member reads, and the
 * 2026-09-25 user test is the record of what happens when those sentences are the operator's:
 * raw server reasons under the seen toggle, a milestone label on a disabled Play button, a
 * source key where a source name belongs. Here each rule is one function with its own test, and
 * the component only decides where the sentence goes.
 */

/**
 * The seen toggle's note (§7.3: the app-side write is authoritative, and the note says what
 * happened to Jellyfin). `seen.set_state` answers `{synced, reason}` with a reason written for
 * §6.7's rail, which prints it verbatim; the card maps it to the member register (decision 486)
 * rather than showing the rail's sentence. The reason wins over `synced`, because decision 210(a)
 * reports a series un-mark as `synced: true` with the reason that nothing was sent.
 *
 * @param {{synced?: boolean, reason?: string | null} | null | undefined} res
 */
export function syncNote(res) {
  const reason = String(res?.reason ?? '');
  if (!reason) return res?.synced ? 'Saved, and Jellyfin is up to date.' : 'Saved here - Jellyfin was not told.';
  if (reason.includes('series unseen is app-only')) {
    return 'Saved here only - Jellyfin keeps its own episode history.';
  }
  if (reason.includes('not on Jellyfin')) {
    return "Saved here - this title isn't in your Jellyfin library.";
  }
  if (reason.includes('re-link required')) {
    return 'Saved here - Jellyfin was not told. Ask an admin to link your account again.';
  }
  if (reason.includes('not linked to a Jellyfin user')) {
    return "Saved here - your account isn't linked to Jellyfin.";
  }
  if (reason.includes('Jellyfin not configured')) return "Saved here - Jellyfin isn't connected.";
  if (reason.includes('secrets unreadable')) {
    return "Saved here - Jellyfin's saved sign-in can't be read. Tell whoever runs Spielplan.";
  }
  if (reason.includes('nothing owed')) return 'Saved.';
  // What is left is an outage or a push already in flight, and both leave the row owed, which
  // §7.3's sweep settles - so this one sentence is true of every remaining cause.
  return 'Saved here - Jellyfin was not updated this time; it catches up on its next sync.';
}

/**
 * Why Play is unavailable, from `actions.play_reason`. Two causes, two sentences: the card used
 * to print "Play needs a linked Jellyfin server - an admin links one in Admin (M1)" for every title
 * outside the library, on an install whose server was linked.
 *
 * @param {string | null | undefined} reason
 */
export function playWhy(reason) {
  if (reason === 'not_in_library') return 'Not in your Jellyfin library.';
  if (reason === 'no_server') return "Jellyfin isn't connected yet - ask whoever runs Spielplan.";
  return '';
}

/**
 * An evidence quote with an ellipsis where it was cut from a longer sentence (C9.6). The rule is
 * `quote.js`'s and the card reads its wording from here, so it is re-exported rather than written
 * twice. Heat's mood.gritty quote read as a negation because nothing said it was a fragment.
 */
export { quoteText } from './quote.js';

const SOURCE_NAMES = {
  blog: 'Blog',
  imdb: 'IMDb',
  metacritic: 'Metacritic',
  plot: 'Plot summary',
  rottentomatoes: 'Rotten Tomatoes',
  rtcritic: 'Rotten Tomatoes critic',
  tmdb: 'TMDB',
  trakt: 'Trakt',
  wiki: 'Wikipedia',
  wikipedia: 'Wikipedia'
};

/**
 * Where a quote came from, by name. The stored key is `<platform>:<n>` - `metacritic:3` is the
 * third Metacritic review in the pack - or `<platform>:<section>`, and the number means nothing to
 * a reader while the section does.
 *
 * @param {string | null | undefined} source
 */
export function sourceLabel(source) {
  const raw = String(source ?? '');
  const cut = raw.indexOf(':');
  const platform = cut === -1 ? raw : raw.slice(0, cut);
  const detail = cut === -1 ? '' : raw.slice(cut + 1);
  const name = SOURCE_NAMES[platform] ?? platform.replaceAll('_', ' ');
  return detail && !/^\d+$/.test(detail) ? `${name} · ${detail.replaceAll('_', ' ')}` : name;
}

// Job spellings that name the same credit within one role class, measured on the real corpus
// (user test 2026-09-25): tmdb's "Original Music Composer" is wikidata's "Composer", and
// "Screenplay" is the writing credit tmdb calls "Writer". What is NOT here is the point of the
// list - Novel, Story, Characters, Teleplay, Co-Director - which are different credits and are
// printed beside the first.
const SYNONYMS = {
  cast: ['actor', 'acting', 'actress'],
  composer: ['composer', 'original music composer', 'music', 'music composer'],
  director: ['director', 'directing'],
  dp: ['director of photography', 'cinematography', 'cinematographer'],
  editor: ['editor', 'film editor', 'editing'],
  prod_designer: ['production design', 'production designer'],
  writer: ['writer', 'writing', 'screenplay', 'screenwriter']
};

/**
 * A credit's job line: its job, then any further job the same person holds in the same role
 * that is a different credit rather than a second spelling. Tolerant of a payload without
 * `role_class`/`jobs`, which is the job alone.
 *
 * @param {{job?: string, jobs?: string[] | null, role_class?: string | null}} credit
 */
export function creditJobs(credit) {
  const job = String(credit?.job ?? '');
  const same = SYNONYMS[credit?.role_class ?? ''] ?? [];
  const seen = new Set([job.toLowerCase()]);
  const primaryIsSynonym = same.includes(job.toLowerCase());
  const extra = [];
  for (const other of credit?.jobs ?? []) {
    const key = String(other).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    if (primaryIsSynonym && same.includes(key)) continue;
    extra.push(other);
  }
  return [job, ...extra].filter(Boolean).join(' · ');
}

/**
 * The keyed-each key for one credit row: person and role class, delimited, falling back to the
 * job for a payload that has no class. `credits_for` returns one row per (person, role class), so
 * the job no longer separates rows - two spellings of one job are one row now.
 *
 * @param {{person_id: number, role_class?: string | null, job?: string}} credit
 */
export function creditKey(credit) {
  return `${credit.person_id}:${credit.role_class ?? credit.job}`;
}

/**
 * §6.1's reveal, as a sentence built from its class rather than from its `text`, which carries
 * the data-voice number beside the phrase. The number is Show the model's (decision 486); the
 * phrase is §6.1's own.
 *
 * @param {{available?: boolean, agreed?: boolean, predicted_label?: string, cdf?: number} | null | undefined} reveal
 */
export function revealLine(reveal) {
  if (!reveal?.available || !reveal.predicted_label) return '';
  return reveal.agreed ? "We'd have guessed the same." : `We'd have guessed ${reveal.predicted_label}.`;
}

/**
 * Decision 487's four answers, worst to best and then Not seen - the order Rate's sweep card draws
 * its verdicts in (proposal 52: "worst → best, matching the stored ordinal"). The card used to
 * read `Liked / Fine / Disliked`, §6.1's list order, so the same three buttons ran in opposite
 * directions two taps apart (U9 of the second household test; decision 517).
 */
export const ANSWERS = [
  { answer: 'disliked', label: 'Disliked' },
  { answer: 'fine', label: 'Fine' },
  { answer: 'liked', label: 'Liked' },
  { answer: 'not_seen', label: 'Not seen' }
];

/**
 * How many credit rows the card shows before "More about this film": the director and the first
 * of the billed cast (`credits_for` orders directing first, then billing). The rest, the platform
 * scores and both DNA tiers fold behind the disclosure - moved, never dropped (decision 517).
 */
export const CREDIT_TOP = 5;

/**
 * The viewer's first language, as the browser reports it: `de` for a `de-DE` phone. Read here
 * rather than in markup so the card and the poster ask the same question.
 */
export function viewerLanguage() {
  const nav = typeof navigator === 'undefined' ? null : navigator;
  const tag = nav?.languages?.[0] ?? nav?.language ?? '';
  return String(tag).slice(0, 2).toLowerCase();
}

/**
 * Which name leads for this viewer, and which rides beside it.
 *
 * A German viewer knows "Wunderschön" and not "Wonderfully Beautiful", its English release title;
 * `title.name` is the corpus's English name everywhere. Where the title's original language is the
 * viewer's own, the original title leads and the English one follows; everywhere else `name` leads
 * as it always has, with the original beside it where it differs (U13 of the second household
 * test; decision 516). A payload without `original_language` keeps `name` in front - the original
 * title alone cannot say which language it is in.
 *
 * @param {{name?: string, original_name?: string | null, original_language?: string | null}} title
 * @param {string} [language]
 */
export function displayNames(title, language = viewerLanguage()) {
  const name = String(title?.name ?? '');
  const original = String(title?.original_name ?? '').trim();
  const differs = Boolean(original) && original.toLowerCase() !== name.toLowerCase();
  if (differs && language && title?.original_language === language) {
    return { primary: original, secondary: name };
  }
  return { primary: name, secondary: differs ? original : '' };
}

/**
 * One tag's evidence with each quote once. Two extraction runs stored Collateral's Metacritic
 * "master poet of nocturnal Los Angeles" twice under one tag, and the card printed it twice
 * (894 such pairs on the household install). The stored rows are untouched; this is the read.
 *
 * @param {{quote?: string, source?: string}[] | null | undefined} evidence
 */
export function dedupeEvidence(evidence) {
  const seen = new Set();
  return (evidence ?? []).filter((e) => {
    const key = String(e?.quote ?? '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * The quoted tier, one block per term. §6.6's parallel extraction writes one term once per
 * provider, and a member read two identical "tense" blocks; the providers are the operator's
 * provenance and stay on the rows (`rows`) for Show the model. §4.1 rule 1 is about the tiers,
 * which stay two lists: this merges rows of ONE tier only.
 *
 * @param {any[] | null | undefined} extracted
 */
export function extractedByTerm(extracted) {
  const byTerm = new Map();
  for (const tag of extracted ?? []) {
    const key = `${tag.facet}:${tag.term}`;
    const block = byTerm.get(key);
    if (block) {
      block.rows.push(tag);
      block.evidence = dedupeEvidence([...block.evidence, ...(tag.evidence ?? [])]);
    } else {
      byTerm.set(key, { ...tag, key, rows: [tag], evidence: dedupeEvidence(tag.evidence) });
    }
  }
  return [...byTerm.values()];
}

// Pacing is the one facet of the vocabulary that is a single scale, so it is the one place a
// contradiction can be named without inventing content: the vocabulary ships no antonyms. Terms
// that sit on neither end (taut, sprawling, talky, steady escalation, ticking-clock urgency) are
// on no pole and contradict nothing.
const PACE_POLE = {
  'pacing.slow_burn': 'slow',
  'pacing.slow_paced': 'slow',
  'pacing.meditative': 'slow',
  'pacing.meandering': 'slow',
  'pacing.unhurried': 'slow',
  'pacing.deliberate': 'slow',
  'pacing.frenetic': 'fast',
  'pacing.fast_paced': 'fast',
  'pacing.high_octane': 'fast',
  'pacing.kinetic': 'fast',
  'pacing.relentless': 'fast',
  'pacing.rapid_fire_delivery': 'fast',
  'pacing.propulsive': 'fast',
  'pacing.breezy': 'fast'
};

/**
 * The title's pace as its own evidence has it: the quoted tier's pole when the quotes hold one,
 * otherwise the pole the inferred tier weighs more. Null when neither says, or the quotes say both.
 */
function paceOf(extracted, projected) {
  const quoted = new Set(extracted.map((t) => PACE_POLE[t.term]).filter(Boolean));
  if (quoted.size === 1) return [...quoted][0];
  if (quoted.size > 1) return null;
  const weight = { slow: 0, fast: 0 };
  for (const p of projected) if (PACE_POLE[p.term]) weight[PACE_POLE[p.term]] += Number(p.weight) || 0;
  if (weight.slow === weight.fast) return null;
  return weight.slow > weight.fast ? 'slow' : 'fast';
}

/**
 * The inferred tier as the card shows it: a term the quotes already carry is shown once, quoted;
 * what one source alone suggests, or what contradicts the title's own pace, is `weak` and folds
 * behind a disclosure of its own; the rest is `strong`. Presentation only - §4.1 rule 2 makes a
 * weight a weight and never a filter, so every chip stays in the payload and on the card, one tap
 * away (U3 and U4 of the second household test; decision 517).
 *
 * @param {any[] | null | undefined} projected
 * @param {any[] | null | undefined} extracted
 */
export function projectedForCard(projected, extracted) {
  const quoted = new Set((extracted ?? []).map((t) => t.term));
  const shown = (projected ?? []).filter((p) => !quoted.has(p.term));
  const pace = paceOf(extracted ?? [], shown);
  const strong = [];
  const weak = [];
  for (const p of shown) {
    const pole = PACE_POLE[p.term];
    const lone = p.weight != null && Math.round(p.weight) <= 1;
    (lone || (pace && pole && pole !== pace) ? weak : strong).push(p);
  }
  return { strong, weak };
}

/**
 * What the card says after an answer landed.
 *
 * @param {string} answer
 */
export function answeredLine(answer) {
  if (answer === 'not_seen') return 'Saved - marked not seen.';
  return `Saved - you ${answer === 'fine' ? 'thought it was fine' : answer + ' it'}.`;
}
