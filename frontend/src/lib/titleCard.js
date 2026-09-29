// The title card's member-facing sentences, one rule per function; the component only places them.

/**
 * The reason wins over `synced`: a series reports `synced: true` with the reason that nothing was
 * sent (decisions 210a, 532).
 *
 * @param {{synced?: boolean, reason?: string | null} | null | undefined} res
 */
export function syncNote(res) {
  const reason = String(res?.reason ?? '');
  if (!reason) return res?.synced ? 'Saved, and Jellyfin is up to date.' : 'Saved here — Jellyfin was not told.';
  if (reason.includes('is app-only')) {
    return 'Saved here only — Jellyfin keeps its own episode history.';
  }
  if (reason.includes('not on Jellyfin')) {
    return "Saved here — this title isn't in your Jellyfin library.";
  }
  if (reason.includes('re-link required')) {
    return 'Saved here — Jellyfin was not told. Ask an admin to link your account again.';
  }
  if (reason.includes('not linked to a Jellyfin user')) {
    return "Saved here — your account isn't linked to Jellyfin.";
  }
  if (reason.includes('Jellyfin not configured')) return "Saved here — Jellyfin isn't connected.";
  if (reason.includes('secrets unreadable')) {
    return "Saved here — Jellyfin's saved sign-in can't be read. Tell whoever runs Spielplan.";
  }
  if (reason.includes('nothing owed')) return 'Saved.';
  // What is left leaves the row owed, which the sweep settles.
  return 'Saved here — Jellyfin was not updated this time; it catches up on its next sync.';
}

/** @param {string | null | undefined} reason */
export function playWhy(reason) {
  if (reason === 'not_in_library') return 'Not in your Jellyfin library.';
  if (reason === 'no_server') return "Jellyfin isn't connected yet — ask whoever runs Spielplan.";
  return '';
}

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
 * The stored key is `<platform>:<n>` or `<platform>:<section>`; the number means nothing to a reader.
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

// Spellings of the same credit per role class; Novel, Story or Teleplay are different credits.
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
 * The job, then any further job in the same role that is a different credit, not a respelling.
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
 * `credits_for` returns one row per (person, role class), so the job no longer separates rows.
 *
 * @param {{person_id: number, role_class?: string | null, job?: string}} credit
 */
export function creditKey(credit) {
  return `${credit.person_id}:${credit.role_class ?? credit.job}`;
}

/**
 * Built from the class, not `text`, whose number is Show the model's (decision 486).
 *
 * @param {{available?: boolean, agreed?: boolean, predicted_label?: string, cdf?: number} | null | undefined} reveal
 */
export function revealLine(reveal) {
  if (!reveal?.available || !reveal.predicted_label) return '';
  return reveal.agreed ? "We'd have guessed the same." : `We'd have guessed ${reveal.predicted_label}.`;
}

// Worst to best, then Not seen: the order Rate's sweep card uses.
export const ANSWERS = [
  { answer: 'disliked', label: 'Disliked' },
  { answer: 'fine', label: 'Fine' },
  { answer: 'liked', label: 'Liked' },
  { answer: 'not_seen', label: 'Not seen' }
];

// Credit rows shown before the fold; `credits_for` orders directing first, then billing.
export const CREDIT_TOP = 5;

// Read here so the card and the poster ask the same question.
export function viewerLanguage() {
  const nav = typeof navigator === 'undefined' ? null : navigator;
  const tag = nav?.languages?.[0] ?? nav?.language ?? '';
  return String(tag).slice(0, 2).toLowerCase();
}

/**
 * The original title leads where its language is the viewer's; without `original_language` the
 * original alone cannot say its language, so `name` leads.
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
 * Each quote once, whitespace and case aside; the stored rows are untouched.
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
 * One block per term across providers; this merges rows of one tier only (§4.1 rule 1).
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

// Pacing is the vocabulary's one single-scale facet, the only place a contradiction can be named.
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

// The quoted pole if the quotes hold one, else the heavier inferred pole; null when unclear.
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
 * Presentation only: weak chips fold, but every chip stays on the card (§4.1 rule 2).
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

/** @param {string} answer */
export function answeredLine(answer) {
  if (answer === 'not_seen') return 'Saved — marked not seen.';
  return `Saved — you ${answer === 'fine' ? 'thought it was fine' : answer + ' it'}.`;
}

/**
 * "Directed by …" from the credits `credits_for` sends, directing first; '' when none directed.
 *
 * @param {{name: string, role_class?: string | null, job?: string}[] | null | undefined} credits
 */
export function directedBy(credits) {
  const names = [
    ...new Set(
      (credits ?? [])
        .filter((c) => c.role_class === 'director' || String(c.job).toLowerCase() === 'director')
        .map((c) => c.name)
    )
  ];
  if (!names.length) return '';
  if (names.length <= 2) return `Directed by ${names.join(' and ')}`;
  return `Directed by ${names.slice(0, 2).join(', ')} and ${names.length - 2} more`;
}

/**
 * The first two genres as a sentence: "Crime, thriller"; '' when the title has none.
 *
 * @param {string[] | null | undefined} genres
 */
export function genreLine(genres) {
  const line = (genres ?? []).slice(0, 2).join(', ').toLowerCase();
  return line.charAt(0).toUpperCase() + line.slice(1);
}

const PLACED_BY = {
  backbone: 'Placed by how people rated it',
  blended: "Placed by how people rated it and by what it's about",
  cold_tower: "Placed by what it's about"
};

/** Where the numbers' coordinate came from, in words; '' for a source with none. */
export function placedBy(source) {
  return PLACED_BY[source] ?? '';
}

const SCORE_WORDS = { critic_score: 'critics', audience_score: 'audience', user_score: 'users' };

/**
 * A score tile's name: the platform, and the audience only where the platform shows two scores.
 *
 * @param {{platform: string, metric: string}} item
 * @param {{platform: string}[]} items
 */
export function scoreLabel(item, items) {
  const name = sourceLabel(item.platform);
  const several = items.filter((i) => i.platform === item.platform).length > 1;
  const word = SCORE_WORDS[item.metric] ?? String(item.metric ?? '').replaceAll('_', ' ');
  return several || !(item.metric in SCORE_WORDS) ? `${name} ${word}`.trim() : name;
}
