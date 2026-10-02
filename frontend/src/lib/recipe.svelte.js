// A recipe (decisions 559 and 560): its films live in `homeFilters.like`/`less` as "245" or
// "6087:mood,sound", so Home's URL carries them. What the picker and the grid said about each film
// (name, poster, its groups' terms) is kept here, by id.
import { facetColour } from '$lib/home.svelte.js';
import { homeFilters } from '$lib/homeFilters.svelte.js';

export const MAX_FILMS = 4;
export const MAX_LENDING = 2;

// The engine's eight groups in its order; a group's dot is its first facet's colour.
export const GROUPS = [
  ['mood', 'Mood', ['mood', 'sensibility']],
  ['look', 'Look', ['visual']],
  ['sound', 'Sound', ['sound']],
  ['pace', 'Pace', ['pacing']],
  ['storytelling', 'Storytelling', ['structure']],
  ['setting', 'Setting', ['place', 'era']],
  ['characters', 'Characters', ['characters']],
  ['themes', 'Themes', ['themes']]
].map(([key, name, facets]) => ({ key, name, facets, colour: facetColour(facets[0]) }));

const GROUP = Object.fromEntries(GROUPS.map((g) => [g.key, g]));
export const groupOf = (key) => GROUP[key];

export const FULL = 'A recipe takes four films at most. Remove one to add another.';

/** @type {Record<number, any>} */
const known = $state({});

const FIELDS = ['kind', 'name', 'year', 'poster_path', 'is_owned', 'sheet'];

/** Keep what a picker row, a twist or the grid's answer says about a recipe film. */
export function remember(film) {
  const id = Number(film?.id ?? film?.title_id);
  if (!Number.isInteger(id) || id <= 0) return;
  const next = { ...known[id], id };
  for (const key of FIELDS) if (film[key] !== undefined && film[key] !== null) next[key] = film[key];
  known[id] = next;
}

function parse(value, sign) {
  const [head, tail = ''] = String(value).split(':');
  const id = Number(head);
  if (!Number.isInteger(id) || id <= 0) return null;
  return { id, sign, groups: [...new Set(tail.split(',').filter((g) => GROUP[g]))] };
}

const serialize = (film) => (film.groups.length ? `${film.id}:${film.groups.join(',')}` : String(film.id));

/** The films as the URL values carry them, likes first, each with what is known about it. */
export function fromParams(f = homeFilters) {
  const out = [];
  for (const [values, sign] of [[f.like, 'like'], [f.less, 'less']]) {
    for (const value of values) {
      const film = parse(value, sign);
      if (film && !out.some((x) => x.id === film.id)) out.push({ name: '', ...known[film.id], ...film });
    }
  }
  return out;
}

export function toParams(films, f = homeFilters) {
  f.like = films.filter((x) => x.sign === 'like').map(serialize);
  f.less = films.filter((x) => x.sign === 'less').map(serialize);
}

export const recipe = {
  get films() {
    return fromParams();
  }
};

const lenders = (films) => films.filter((x) => x.groups.length);

/** Adds a film, or moves one already there to `sign`; the reason when the recipe is full. */
export function addFilm(film, sign = 'like') {
  remember(film);
  const id = Number(film.id ?? film.title_id);
  const films = fromParams();
  if (films.some((x) => x.id === id)) {
    setSign(id, sign);
    return null;
  }
  if (films.length >= MAX_FILMS) return FULL;
  toParams([...films, { id, sign, groups: [] }]);
  return null;
}

export function removeFilm(id) {
  toParams(fromParams().filter((x) => x.id !== id));
}

export function setSign(id, sign) {
  toParams(fromParams().map((x) => (x.id === id ? { ...x, sign } : x)));
}

/**
 * The groups a film lends. One film per group: another film holding one of them loses it, and
 * goes back to the whole of itself, with its sign, when that was its last (decision 560 item 6).
 */
export function setGroups(id, groups) {
  const mine = [...new Set(groups)].filter((g) => GROUP[g]);
  const films = fromParams();
  const others = lenders(films.filter((x) => x.id !== id));
  const film = films.find((x) => x.id === id);
  if (!film) return null;
  if (mine.length && !film.groups.length && others.length >= MAX_LENDING) return lendingFull(others);
  toParams(
    films.map((x) =>
      x.id === id ? { ...x, groups: mine } : { ...x, groups: x.groups.filter((g) => !mine.includes(g)) }
    )
  );
  return null;
}

/** A new recipe of this one film, liked, with the Filters open on it; the search goes, the filters stay. */
export function startWith(film) {
  remember(film);
  homeFilters.q = '';
  homeFilters.like = [String(film.id ?? film.title_id)];
  homeFilters.less = [];
  homeFilters.panelOpen = true;
}

function join(names, amp = true) {
  if (names.length < 2) return names.join('');
  return `${names.slice(0, -1).join(', ')}${amp ? ' & ' : ' and '}${names.at(-1)}`;
}

const nameOf = (film) => film.name || '…';
const lower = (keys) => join(keys.map((k) => GROUP[k].name.toLowerCase()));
// "Fargo's mood", "Prisoners' mood".
const owner = (name) => (/s$/i.test(name) ? `${name}'` : `${name}'s`);
const theirs = (film) => `${owner(nameOf(film))} ${lower(film.groups)}`;

function groupsLabel(keys) {
  const names = keys.map((k) => GROUP[k].name);
  return names.length <= 3 ? join(names) : `${names[0]}, ${names[1]} & ${names.length - 2} more`;
}

/** "Like Knives Out", "Less like Star Wars", "Mood, Sound & Look like Obsession". */
export function chipLabel(film) {
  const like = film.sign === 'like';
  if (!film.groups.length) return `${like ? 'Like' : 'Less like'} ${nameOf(film)}`;
  return `${groupsLabel(film.groups)} ${like ? 'like' : 'less like'} ${nameOf(film)}`;
}

/** The chosen groups' terms, a few from each in turn, quoted first; '' for a whole film. */
export function chipTerms(film, k = 6) {
  const lists = film.groups.map((g) => {
    const row = (film.sheet ?? []).find((r) => r.group === g);
    return row ? [...row.quoted, ...row.inferred].map((t) => t.label) : [];
  });
  const out = [];
  for (let i = 0; out.length < k && lists.some((l) => i < l.length); i++) {
    for (const list of lists) if (i < list.length && out.length < k) out.push(list[i]);
  }
  return out.join(', ');
}

/** The head's sentence once a group is lent: "Knives Out, with Fargo's mood in place of its own". */
export function sentence(films = fromParams()) {
  const whole = films.filter((x) => x.sign === 'like' && !x.groups.length).map(nameOf);
  const lent = films.filter((x) => x.sign === 'like' && x.groups.length).map(theirs);
  const away = films.filter((x) => x.sign === 'less' && x.groups.length).map(theirs);
  if (!lent.length && !away.length) return '';
  const parts = [];
  const base = join(whole, false);
  if (lent.length) {
    parts.push(
      base
        ? `${base}, with ${join(lent, false)} in place of ${whole.length > 1 ? 'their' : 'its'} own`
        : `Just ${join(lent, false)}`
    );
  } else if (base) parts.push(base);
  if (away.length) parts.push(`pushed away from ${join(away, false)}`);
  const line = parts.join(', ');
  return line.charAt(0).toUpperCase() + line.slice(1);
}

/** What this choice in a film's sheet does, in the words of board MA-2. */
export function groupNote(film, groups, sign, films = fromParams()) {
  const others = films.filter((x) => x.id !== film.id && x.sign === 'like' && !x.groups.length);
  const base = join(others.map(nameOf), false);
  if (!groups.length) {
    if (sign === 'less') return `Pushes away films like ${nameOf(film)}.`;
    return base ? `${nameOf(film)} counts as much as ${base}.` : `Every part of ${nameOf(film)} counts.`;
  }
  const parts = `${owner(nameOf(film))} ${lower(groups)}`;
  if (sign === 'less') {
    return base
      ? `Pushes away the parts of ${parts} that ${base} ${others.length > 1 ? 'do' : 'does'} not share.`
      : `Pushes away ${parts}.`;
  }
  if (!base) return `Only ${parts} ${groups.length > 1 ? 'count' : 'counts'}.`;
  return `${base} ${others.length > 1 ? 'keep' : 'keeps'} everything but ${others.length > 1 ? 'their' : 'its'} own ${lower(groups)}.`;
}

/** The sheet's foot: a film lending any number of groups is one of the two that may lend. */
export function lendNote(film, films = fromParams()) {
  const other = lenders(films).find((x) => x.id !== film.id);
  const line = `However many parts you take, ${nameOf(film)} counts as one of the ${MAX_LENDING} films that can lend parts.`;
  return other ? `${line} ${nameOf(other)} is the other.` : line;
}

const lendingFull = (others) => `${join(others.map(nameOf), false)} already lend parts`;

/** A group row of a film's sheet: disabled with its reason, or open with a note on who holds it. */
export function groupRow(film, row, films = fromParams()) {
  if (!row.offered) {
    const only = [...row.quoted, ...row.inferred][0];
    return { disabled: true, reason: only ? `Only one term here: ${only.label}` : 'No terms here' };
  }
  const others = lenders(films.filter((x) => x.id !== film.id));
  if (!film.groups.length && others.length >= MAX_LENDING) {
    return { disabled: true, reason: lendingFull(others) };
  }
  const holder = others.find((x) => x.groups.includes(row.group));
  return { disabled: false, reason: holder ? `${GROUP[row.group].name} comes from ${nameOf(holder)} now` : '' };
}

/** Twists only where one more lent group stays inside the limits (decision 560 item 7). */
export function twistsOffered(films = fromParams()) {
  return films.length > 0 && films.length < MAX_FILMS && lenders(films).length < MAX_LENDING;
}

/** Adds a twist's group of its film, liked; returns the Undo that puts the recipe back. */
export function applyTwist(twist) {
  const before = { like: [...homeFilters.like], less: [...homeFilters.less] };
  const film = { id: twist.title_id, name: twist.name, year: twist.year, poster_path: twist.poster_path };
  if (addFilm(film, 'like') === null) setGroups(film.id, [twist.group]);
  return () => {
    homeFilters.like = before.like;
    homeFilters.less = before.less;
  };
}

const credit = (w) => (w.groups.length ? `${owner(w.name)} ${lower(w.groups)}` : w.name);

/** The card's line for a recipe result: "From Dune: prestige address · but pulp, like Star Wars". */
export function whyLine(why = []) {
  return why
    .filter((w) => w.terms?.length)
    .map((w) => {
      const terms = w.terms.map((t) => t.label).join(', ');
      if (!w.like) return `but ${terms}, like ${credit(w)}`;
      return w.groups.length ? `${credit(w)}: ${terms}` : `From ${w.name}: ${terms}`;
    })
    .join(' · ');
}

/** One poster caption per liked film: the strongest term the two share. */
export function captions(why = []) {
  return why
    .filter((w) => w.like && w.terms?.length)
    .map((w) => ({ title_id: w.title_id, from: `From ${credit(w)}: `, term: w.terms[0].label }));
}
