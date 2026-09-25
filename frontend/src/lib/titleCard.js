/**
 * The title card's wording, as pure functions. Spec v2.1 §6.0, §6.8, §7.3; decisions 486, 487.
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
 * An evidence quote with an ellipsis where it was cut from a longer sentence. One rule for both
 * ends: a quote that opens on a lower-case letter starts mid-sentence, and one that does not end
 * on terminal punctuation stops mid-sentence. The stored quote is untouched, so §4.1 rule 1's
 * verification and §6.6's adjudication still read the exact span; this is typography only.
 * Heat's mood.gritty quote read as a negation because nothing said it was a fragment.
 *
 * @param {string | null | undefined} quote
 */
export function quoteText(quote) {
  const text = String(quote ?? '').trim();
  if (!text) return '';
  const lead = /^["'“‘(]*\p{Ll}/u.test(text) ? '…' : '';
  const tail = /[.!?…]["'”’)\]]*$/u.test(text) ? '' : '…';
  return lead + text + tail;
}

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

/** Decision 487's four answers: §6.1's `Liked / Fine / Disliked`, in its order, then Not seen. */
export const ANSWERS = [
  { answer: 'liked', label: 'Liked' },
  { answer: 'fine', label: 'Fine' },
  { answer: 'disliked', label: 'Disliked' },
  { answer: 'not_seen', label: 'Not seen' }
];

/**
 * What the card says after an answer landed.
 *
 * @param {string} answer
 */
export function answeredLine(answer) {
  if (answer === 'not_seen') return 'Saved - marked not seen.';
  return `Saved - you ${answer === 'fine' ? 'thought it was fine' : answer + ' it'}.`;
}
