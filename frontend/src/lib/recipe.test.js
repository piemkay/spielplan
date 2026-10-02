import { afterEach, describe, expect, it } from 'vitest';

import { homeFilters, resetHomeFilters } from './homeFilters.svelte.js';
import {
  FULL,
  GROUPS,
  addFilm,
  applyTwist,
  captions,
  chipLabel,
  chipTerms,
  fromParams,
  groupNote,
  groupRow,
  lendNote,
  recipe,
  remember,
  removeFilm,
  sentence,
  setGroups,
  setSign,
  startWith,
  toParams,
  twistsOffered,
  whyLines
} from './recipe.svelte.js';

const KNIVES = { id: 245, kind: 'movie', name: 'Knives Out', year: 2019 };
const FARGO = { id: 275, kind: 'movie', name: 'Fargo', year: 1996 };
const WARS = { id: 11, kind: 'movie', name: 'Star Wars', year: 1977 };
const OBSESSION = { id: 6087, kind: 'movie', name: 'Obsession', year: 1976 };
const ZODIAC = { id: 1949, kind: 'movie', name: 'Zodiac', year: 2007 };

const term = (label, facet = 'mood') => ({ term: `${facet}.${label}`, label, facet });
const sheetRow = (group, quoted, inferred = [], offered = quoted.length + inferred.length >= 2) => ({
  group, name: GROUPS.find((g) => g.key === group).name, offered, quoted: quoted.map((l) => term(l)),
  inferred: inferred.map((l) => term(l))
});

afterEach(resetHomeFilters);

const film = (id) => recipe.films.find((f) => f.id === id);

describe('a recipe in the URL', () => {
  it('writes each film as its id, or its id and the groups it lends, likes and less likes apart', () => {
    addFilm(KNIVES, 'like');
    addFilm(OBSESSION, 'like');
    addFilm(WARS, 'less');
    setGroups(OBSESSION.id, ['mood', 'sound', 'look']);
    expect(homeFilters.like).toEqual(['245', '6087:mood,sound,look']);
    expect(homeFilters.less).toEqual(['11']);
  });

  it('reads them back, dropping what is not a film id and a group the engine lacks', () => {
    Object.assign(homeFilters, { like: ['245', 'heat', '6087:mood,smell', '245'], less: ['11:look'] });
    expect(fromParams().map(({ id, sign, groups }) => ({ id, sign, groups }))).toEqual([
      { id: 245, sign: 'like', groups: [] },
      { id: 6087, sign: 'like', groups: ['mood'] },
      { id: 11, sign: 'less', groups: ['look'] }
    ]);
  });

  it('round-trips through toParams', () => {
    Object.assign(homeFilters, { like: ['245', '6087:mood,sound'], less: ['11'] });
    const films = fromParams();
    resetHomeFilters();
    toParams(films);
    expect(homeFilters.like).toEqual(['245', '6087:mood,sound']);
    expect(homeFilters.less).toEqual(['11']);
  });

  it('names a film from what the picker or the grid said about it', () => {
    homeFilters.like = ['9001'];
    expect(film(9001).name).toBe('');
    remember({ title_id: 9001, name: 'Gosford Park', year: 2001, sheet: [] });
    expect(film(9001)).toMatchObject({ name: 'Gosford Park', year: 2001, sign: 'like' });
  });
});

describe('the limits (decision 560 item 6)', () => {
  it('takes four films and refuses a fifth with its reason', () => {
    for (const f of [KNIVES, FARGO, WARS, OBSESSION]) expect(addFilm(f)).toBeNull();
    expect(addFilm(ZODIAC)).toBe(FULL);
    expect(recipe.films).toHaveLength(4);
    expect(addFilm(WARS, 'less'), 'a film already in moves to the other sign').toBeNull();
    expect(film(WARS.id).sign).toBe('less');
  });

  it('lets two films lend groups, a film lending several counting once', () => {
    for (const f of [KNIVES, FARGO, OBSESSION]) addFilm(f);
    expect(setGroups(OBSESSION.id, ['mood', 'sound', 'look'])).toBeNull();
    expect(setGroups(FARGO.id, ['pace'])).toBeNull();
    expect(setGroups(KNIVES.id, ['themes'])).toBe('Fargo and Obsession already lend parts');
    expect(film(KNIVES.id).groups).toEqual([]);
  });

  it('gives a group to one film: a second film takes it over', () => {
    addFilm(KNIVES);
    addFilm(FARGO);
    addFilm(OBSESSION, 'less');
    setGroups(OBSESSION.id, ['mood', 'look']);
    setGroups(FARGO.id, ['mood']);
    expect(film(FARGO.id).groups).toEqual(['mood']);
    expect(film(OBSESSION.id).groups).toEqual(['look']);
  });

  it('sends a film whose last group was taken back to all of itself, with its sign', () => {
    addFilm(KNIVES);
    addFilm(FARGO);
    addFilm(OBSESSION, 'less');
    setGroups(OBSESSION.id, ['mood']);
    setGroups(FARGO.id, ['mood']);
    expect(film(OBSESSION.id)).toMatchObject({ groups: [], sign: 'less' });
    expect(homeFilters.less).toEqual(['6087']);
  });

  it('keeps the groups when the sign flips, and drops a removed film', () => {
    addFilm(KNIVES);
    addFilm(FARGO);
    setGroups(FARGO.id, ['mood']);
    setSign(FARGO.id, 'less');
    expect(homeFilters.less).toEqual(['275:mood']);
    removeFilm(KNIVES.id);
    expect(homeFilters.like).toEqual([]);
  });
});

describe('what a chip says', () => {
  it('names the film, its sign and the groups it lends', () => {
    expect(chipLabel({ ...KNIVES, sign: 'like', groups: [] })).toBe('Like Knives Out');
    expect(chipLabel({ ...WARS, sign: 'less', groups: [] })).toBe('Less like Star Wars');
    expect(chipLabel({ ...OBSESSION, sign: 'like', groups: ['mood', 'sound', 'look'] })).toBe(
      'Mood, Sound & Look like Obsession'
    );
    expect(chipLabel({ ...FARGO, sign: 'less', groups: ['mood'] })).toBe('Mood less like Fargo');
    expect(chipLabel({ ...FARGO, sign: 'like', groups: ['mood', 'look', 'pace', 'themes'] })).toBe(
      'Mood, Look & 2 more like Fargo'
    );
  });

  it("lists the lent groups' terms a few from each in turn, and none for a whole film", () => {
    const sheet = [sheetRow('mood', ['dark comedy', 'deadpan & dry'], ['bleak']), sheetRow('look', ['muted'], ['stylized'])];
    expect(chipTerms({ ...FARGO, groups: ['mood', 'look'], sheet })).toBe(
      'dark comedy, muted, deadpan & dry, stylized, bleak'
    );
    expect(chipTerms({ ...FARGO, groups: [], sheet })).toBe('');
  });
});

describe("the recipe's head", () => {
  const as = (f, sign, groups = []) => ({ ...f, sign, groups });

  it('says what replace did once a group is lent', () => {
    expect(sentence([as(KNIVES, 'like'), as(FARGO, 'like', ['mood'])])).toBe(
      "Knives Out, with Fargo's mood in place of its own"
    );
    expect(sentence([as(KNIVES, 'like'), as(ZODIAC, 'like'), as(OBSESSION, 'like', ['mood', 'sound', 'look'])])).toBe(
      "Knives Out and Zodiac, with Obsession's mood, sound & look in place of their own"
    );
    expect(sentence([as(KNIVES, 'like'), as({ id: 2, name: 'Prisoners' }, 'like', ['mood'])])).toBe(
      "Knives Out, with Prisoners' mood in place of its own"
    );
    expect(sentence([as(KNIVES, 'like'), as(WARS, 'less', ['look'])])).toBe(
      "Knives Out, pushed away from Star Wars' look"
    );
  });

  it('says nothing while every film is whole', () => {
    expect(sentence([as(KNIVES, 'like'), as(WARS, 'less')])).toBe('');
  });
});

describe("a film's sheet", () => {
  const as = (f, sign, groups = []) => ({ ...f, sign, groups });

  it('says what each choice does, in the words of board MA-2', () => {
    const films = [as(KNIVES, 'like'), as(FARGO, 'like')];
    expect(groupNote(films[1], [], 'like', films)).toBe('Fargo counts as much as Knives Out.');
    expect(groupNote(films[1], [], 'less', films)).toBe('Pushes away films like Fargo.');
    expect(groupNote(films[1], ['mood'], 'like', films)).toBe('Knives Out keeps everything but its own mood.');
    expect(groupNote(films[1], ['mood', 'look'], 'less', films)).toBe(
      "Pushes away the parts of Fargo's mood & look that Knives Out does not share."
    );
  });

  it('counts a film among the two that can lend, naming the other', () => {
    const films = [as(KNIVES, 'like'), as(FARGO, 'like', ['mood']), as(OBSESSION, 'like', ['look'])];
    expect(lendNote(films[1], films)).toBe(
      'However many parts you take, Fargo counts as one of the 2 films that can lend parts. Obsession is the other.'
    );
    expect(lendNote(films[0], films.slice(0, 2))).toBe(
      'However many parts you take, Knives Out counts as one of the 2 films that can lend parts. Fargo is the other.'
    );
    expect(lendNote(films[0], films.slice(0, 1))).toBe(
      'However many parts you take, Knives Out counts as one of the 2 films that can lend parts.'
    );
  });

  it('names the two films that lend once a third cannot, and how to free a place', () => {
    const films = [as(OBSESSION, 'like', ['mood']), as(FARGO, 'like', ['setting']), as(WARS, 'less')];
    expect(lendNote(films[2], films)).toBe(
      'Obsession and Fargo are the 2 films that can lend parts. To take parts of Star Wars, first choose ' +
        'All of Obsession or All of Fargo.'
    );
  });

  it('disables a thin group with its term, and every group once two other films lend', () => {
    const films = [as(KNIVES, 'like'), as(FARGO, 'like', ['mood']), as(OBSESSION, 'like', ['look'])];
    expect(groupRow(films[0], sheetRow('storytelling', [], ['procedural']), films)).toEqual({
      disabled: true, reason: 'Only one term here: procedural'
    });
    expect(groupRow(films[0], sheetRow('sound', []), films).reason).toBe('No terms here');
    expect(groupRow(films[0], sheetRow('pace', ['tense', 'slow']), films)).toEqual({
      disabled: true, reason: 'Fargo and Obsession already lend parts'
    });
  });

  it('leaves a group another film lends open, saying who holds it', () => {
    const films = [as(KNIVES, 'like'), as(FARGO, 'like', ['mood'])];
    expect(groupRow(films[0], sheetRow('mood', ['tense', 'witty']), films)).toEqual({
      disabled: false, reason: 'Mood comes from Fargo now'
    });
  });
});

describe('twists', () => {
  it('are offered only while one more lent group stays inside the limits', () => {
    addFilm(KNIVES);
    expect(twistsOffered()).toBe(true);
    addFilm(FARGO);
    addFilm(OBSESSION);
    setGroups(FARGO.id, ['mood']);
    setGroups(OBSESSION.id, ['look']);
    expect(twistsOffered(), 'two films lend').toBe(false);
    setGroups(OBSESSION.id, []);
    addFilm(WARS, 'less');
    expect(twistsOffered(), 'four films').toBe(false);
    resetHomeFilters();
    expect(twistsOffered(), 'no recipe').toBe(false);
  });

  it('add a group of a film, and Undo puts the recipe back as it was', () => {
    addFilm(KNIVES);
    addFilm(WARS, 'less');
    const undo = applyTwist({ title_id: 694, name: 'The Shining', year: 1980, group: 'mood' });
    expect(homeFilters.like).toEqual(['245', '694:mood']);
    expect(chipLabel(film(694))).toBe('Mood like The Shining');
    undo();
    expect(homeFilters.like).toEqual(['245']);
    expect(homeFilters.less).toEqual(['11']);
  });
});

describe('More like this on Home (decision 559 item 7)', () => {
  it('starts a new recipe of the title, liked, with the Filters open, the search gone and the filters kept', () => {
    Object.assign(homeFilters, { q: 'fargo', genre: 'Crime', like: ['245'], less: ['11'] });
    startWith(FARGO);
    expect(homeFilters).toMatchObject({ q: '', genre: 'Crime', like: ['275'], less: [], panelOpen: true });
    expect(chipLabel(film(FARGO.id))).toBe('Like Fargo');
  });
});

describe("a result's why (decision 559 item 5)", () => {
  const why = [
    { title_id: 245, name: 'Knives Out', groups: [], like: true, terms: [term('murder mystery'), term('grand estate')] },
    { title_id: 275, name: 'Fargo', groups: ['mood'], like: true, terms: [term('deadpan & dry')] },
    { title_id: 11, name: 'Star Wars', groups: [], like: false, terms: [term('pulp')] }
  ];

  it('gives each credited film its own line: a liked film and its shared terms, a less-liked one still carried', () => {
    expect(whyLines(why)).toEqual([
      { title_id: 245, name: 'Knives Out', text: 'From Knives Out: murder mystery, grand estate', less: false },
      { title_id: 275, name: 'Fargo', text: "Fargo's mood: deadpan & dry", less: false },
      { title_id: 11, name: 'Star Wars', text: 'but pulp, like Star Wars', less: true }
    ]);
    expect(whyLines([{ ...why[0], terms: [] }])).toEqual([]);
  });

  it('captions a poster once per liked film with the strongest term they share', () => {
    expect(captions(why)).toEqual([
      { title_id: 245, from: 'From Knives Out: ', term: 'murder mystery' },
      { title_id: 275, from: "From Fargo's mood: ", term: 'deadpan & dry' }
    ]);
  });
});
