"""Decisions 559 and 560's arithmetic on synthetic tables: no database."""

from __future__ import annotations

import math

import numpy as np
import pytest

from spielplan.home import mix
from spielplan.home.mix import Ingredient

FACETS = ("mood", "sensibility", "register", "visual", "sound", "pacing", "structure", "place", "era",
          "characters", "themes")


def filler(n: int, start: int = 1000) -> dict[int, list[str]]:
    """Owned titles carrying one common term, so a term on a handful of films is rare."""
    return {start + i: ["register.plain"] for i in range(n)}


def make(films, *, owned=None, votes=None, directors=None, people=(), review=None) -> mix.Table:
    """`films`: id -> terms; a trailing `!` marks a quoted term (naming rank 0.9, inferred 0.2)."""
    terms = sorted({t.rstrip("!") for names in films.values() for t in names})
    at = {t: i for i, t in enumerate(terms)}
    rows = []
    for tid in sorted(films):
        names = sorted({t.rstrip("!") for t in films[tid]})
        quoted = {t.rstrip("!") for t in films[tid] if t.endswith("!")}
        rows.append((tid, [at[n] for n in names], [n in quoted for n in names],
                     [0.9 if n in quoted else 0.2 for n in names]))
    owned = set(films) if owned is None else set(owned)
    return mix.Table.build(
        terms=terms, labels=[t.split(".")[1] for t in terms], term_facets=[t.split(".")[0] for t in terms],
        facets=FACETS, colours={}, rows=rows, votes=votes or {}, directors=directors or {},
        people=people, review=review or {}, owned=owned, n_owned=len(owned),
    )


def ops(t: mix.Table, recipe) -> dict[int, mix.Operand]:
    return {i.title_id: t.operand(i.title_id) for i in recipe}


def ranked(t: mix.Table, recipe) -> list[int]:
    return [int(t.ids[r]) for r in mix.rank(t, recipe, ops(t, recipe))]


def score_of(t: mix.Table, recipe, title_id: int) -> float:
    score, _gate = mix.dna_score(t, recipe, ops(t, recipe))
    return float(score[t.row_of[title_id]])


def cosine(t: mix.Table, a: int, b: int, groups=None, *, facets=FACETS) -> float:
    """Decision 513's cosine, recomputed from term names, both sides read over `groups` (or `facets`)."""
    if groups:
        facets = {f for g in groups for f in mix.GROUPS[g]}

    def weights(title_id):
        op = t.operand(title_id)
        return {int(c): float(t.idf[c]) for c in op.cols if t.facets[t.term_facet[c]] in facets}

    wa, wb = weights(a), weights(b)
    dot = sum(w * w for c, w in wa.items() if c in wb)
    norm = math.sqrt(sum(w * w for w in wa.values())) * math.sqrt(sum(w * w for w in wb.values()))
    return dot / norm if norm else 0.0


# A = a heist whodunit with a dark, tense mood; B = a cosy family film.
A = ["mood.dark", "sensibility.tense", "themes.heist", "structure.whodunit"]
B = ["mood.warm", "mood.cozy", "visual.pastel", "visual.soft", "themes.family"]


def test_replace_takes_a_lent_group_out_of_every_whole_film_like():
    """Like A · Mood like B: A's mood no longer admits a result (decision 560 item 4)."""
    t = make({
        1: A, 2: B,
        10: ["mood.dark", "sensibility.tense", "mood.warm"],
        11: ["themes.heist", "structure.whodunit", "mood.cozy"],
        **filler(30),
    })
    assert {10, 11} <= set(ranked(t, [Ingredient(1)]))
    mixed = ranked(t, [Ingredient(1), Ingredient(2, ("mood",))])
    assert 11 in mixed and 10 not in mixed
    # A's factor is read over its unclaimed facets only, the result's terms as well as A's.
    unclaimed = [f for f in FACETS if f not in mix.GROUPS["mood"]]
    expected = math.sqrt(cosine(t, 11, 1, facets=unclaimed) * cosine(t, 11, 2, ["mood"]))
    assert score_of(t, [Ingredient(1), Ingredient(2, ("mood",))], 11) == pytest.approx(expected)


def test_a_films_lent_groups_are_one_factor_over_their_union():
    t = make({2: B, 12: ["mood.warm", "visual.pastel", "visual.soft", "themes.family"], **filler(30)})
    lent = [Ingredient(2, ("mood", "look"))]
    union = cosine(t, 12, 2, ["mood", "look"])
    apart = math.sqrt(cosine(t, 12, 2, ["mood"]) * cosine(t, 12, 2, ["look"]))
    assert union != pytest.approx(apart)
    assert score_of(t, lent, 12) == pytest.approx(union)


def test_a_film_lending_several_groups_counts_once_against_the_limit():
    t = make({1: A, 2: B, 3: ["sound.choral", "sound.drone", "pacing.slow", "pacing.languid"],
              4: ["pacing.fast", "pacing.kinetic", "place.desert", "place.city"]})
    fine = [Ingredient(1), Ingredient(2, ("mood", "look")), Ingredient(3, ("sound",))]
    mix.check(t, fine, ops(t, fine))
    over = [*fine, Ingredient(4, ("pace",))]
    with pytest.raises(mix.MixRefused) as refused:
        mix.check(t, over, ops(t, over))
    assert refused.value.reason == "too_many_lending"


def test_the_geometric_mean_is_zero_when_a_factor_is():
    t = make({1: A, 2: B, 13: ["themes.heist", "structure.whodunit"], **filler(30)})
    assert score_of(t, [Ingredient(1)], 13) > 0
    assert score_of(t, [Ingredient(1), Ingredient(2)], 13) == 0
    assert 13 not in ranked(t, [Ingredient(1), Ingredient(2)])


def test_a_group_less_like_pushes_only_terms_no_liked_film_carries():
    """Decision 560 item 5; a whole-film less-like subtracts plainly (decision 559)."""
    less = ["mood.dark", "mood.grim", "themes.war"]
    t = make({1: A, 3: less, 14: ["themes.heist", "structure.whodunit", "mood.dark"], **filler(30)})
    alone = score_of(t, [Ingredient(1)], 14)
    by_group = score_of(t, [Ingredient(1), Ingredient(3, ("mood",), like=False)], 14)
    whole = score_of(t, [Ingredient(1), Ingredient(3, like=False)], 14)
    assert by_group == pytest.approx(alone), "dark is A's too, so less of 3's mood leaves it alone"
    assert whole == pytest.approx(alone - mix.LESS * cosine(t, 14, 3))
    assert whole < alone
    # A less-like gates nothing: 14 still ranks.
    assert 14 in ranked(t, [Ingredient(1), Ingredient(3, like=False)])


def test_a_survivor_shares_two_rare_terms_with_each_whole_like_and_one_with_each_lent_group():
    t = make({
        1: A, 2: B,
        20: ["themes.heist", "register.plain", "mood.warm"],          # one rare term of A
        21: ["themes.heist", "structure.whodunit"],                   # two, but none of B's mood
        22: ["themes.heist", "structure.whodunit", "mood.cozy"],      # two, and one of B's mood
        **filler(30),
    })
    assert t.idf[t.terms.index("register.plain")] < mix.RARE_IDF
    alone = ranked(t, [Ingredient(1)])
    assert 20 not in alone and {21, 22} <= set(alone)
    lent = ranked(t, [Ingredient(1), Ingredient(2, ("mood",))])
    assert lent == [22]


def test_ties_go_to_the_lower_title_id():
    t = make({1: A, 31: ["themes.heist", "structure.whodunit"], 30: ["themes.heist", "structure.whodunit"],
              **filler(30)})
    assert ranked(t, [Ingredient(1)]) == [30, 31]


def _unit(x: float) -> np.ndarray:
    return np.array([x, math.sqrt(1 - x * x)], dtype=np.float32)


def test_review_text_blends_over_whole_film_likes_only():
    """Identical DNA: the review vector decides for a whole-film like, never for a lent group."""
    films = {2: B, 40: ["mood.warm", "mood.cozy"], 41: ["mood.warm", "mood.cozy"], **filler(30)}
    review = {2: _unit(1.0), 40: _unit(0.1), 41: _unit(0.95)}
    t = make(films, review=review)
    assert ranked(t, [Ingredient(2)])[:2] == [41, 40]
    assert ranked(t, [Ingredient(2, ("mood",))])[:2] == [40, 41]


def test_a_result_without_review_text_ranks_on_dna_alone_at_full_weight():
    """50 and 52 tie on DNA above the mean; 50's review sits at its mean, so a blend leaves it at 0.7 of
    its DNA, while 52 (no review) keeps all of its."""
    films = {
        2: B,
        50: ["mood.warm", "mood.cozy", "visual.pastel"],
        52: ["mood.warm", "mood.cozy", "visual.pastel"],
        53: ["mood.warm", "mood.cozy"],
        54: ["mood.warm", "mood.cozy"],
        **filler(30),
    }
    review = {2: _unit(1.0), 50: _unit(0.5), 53: _unit(0.9), 54: _unit(0.1)}
    t = make(films, review=review)
    assert ranked(t, [Ingredient(2)])[:2] == [52, 50]


def test_a_recipe_with_no_review_text_ranks_on_dna():
    films = {2: B, 40: ["mood.warm", "mood.cozy"], 41: ["mood.warm", "mood.cozy"], **filler(30)}
    t = make(films, review={40: _unit(0.1), 41: _unit(0.95)})
    assert ranked(t, [Ingredient(2)])[:2] == [40, 41]


def test_beyond_the_library_only_well_known_titles_are_read():
    films = {1: A, 60: ["themes.heist", "structure.whodunit"], 61: ["themes.heist", "structure.whodunit"],
             **filler(30)}
    t = make(films, owned=set(films) - {60, 61}, votes={60: mix.WELL_KNOWN_VOTES, 61: 24_999})
    assert ranked(t, [Ingredient(1)]) == [60]


def test_no_liked_film_ranks_nothing():
    t = make({1: A, 21: ["themes.heist", "structure.whodunit"], **filler(30)})
    assert ranked(t, [Ingredient(1, like=False)]) == []


def test_the_director_cap_folds_a_directors_third_film_and_counts_each_director():
    """r2 has two directors, so it counts for both: Y's films are r2 and r3, and r5 folds."""
    films = {i: ["mood.dark", "themes.heist"] for i in range(1, 7)}
    t = make(films, directors={1: [0], 2: [0, 1], 3: [1], 4: [0], 5: [1], 6: [0, 1]},
             people=[((301,), "X"), ((302,), "Y")])
    cells = mix.director_cap(t, [t.row_of[i] for i in range(1, 7)])
    shown = [int(t.ids[c]) for c in cells if isinstance(c, int)]
    folds = [c for c in cells if isinstance(c, mix.Fold)]
    assert shown == [1, 2, 3]
    assert [(f.directors, [int(t.ids[r]) for r in f.rows]) for f in folds] == [((0,), [4, 6]), ((1,), [5])]
    assert cells.index(folds[0]) == 3, "the fold stands where the director's third film stood"


@pytest.mark.parametrize(
    ("recipe", "reason"),
    [
        ([Ingredient(1), Ingredient(2), Ingredient(3), Ingredient(4), Ingredient(5)], "too_many_films"),
        ([Ingredient(1, ("colour",))], "unknown_group"),
        ([Ingredient(1), Ingredient(1, like=False)], "bad_ingredient"),
        ([Ingredient(9)], "no_dna"),
        ([Ingredient(404)], "no_dna"),
        ([Ingredient(1, ("mood",)), Ingredient(2, ("mood",), like=False)], "group_taken_twice"),
        ([Ingredient(1, ("look",))], "group_too_thin"),
        ([Ingredient(1, ("mood",)), Ingredient(2, ("look",)), Ingredient(3, ("pace",))],
         "too_many_lending"),
    ],
)
def test_check_refuses_outside_the_limits(recipe, reason):
    t = make({1: A, 2: B, 3: ["pacing.slow", "pacing.languid"], 4: A, 5: B, 9: ["mood.dark"]})
    with pytest.raises(mix.MixRefused) as refused:
        mix.check(t, recipe, {i.title_id: t.operand(i.title_id) for i in recipe if i.title_id in t.row_of})
    assert refused.value.reason == reason
    assert refused.value.detail()["reason"] == reason


@pytest.mark.parametrize("value", ["abc", "12:", ":mood", ""])
def test_an_unreadable_ingredient_is_refused(value):
    with pytest.raises(mix.MixRefused) as refused:
        mix.ingredient(value, like=True)
    assert refused.value.reason == "bad_ingredient"


def test_an_ingredient_reads_its_id_and_groups():
    assert mix.ingredient("6087:mood,sound", like=False) == Ingredient(6087, ("mood", "sound"), False)
    assert mix.ingredient("245", like=True) == Ingredient(245)


def test_the_derived_line_names_quoted_terms_first():
    t = make({1: ["mood.dark!", "themes.heist", "structure.whodunit!", "sensibility.tense"],
              3: ["mood.grim", "themes.war!", "mood.dark"], **filler(30)})
    more, less = mix.derived(t, [Ingredient(1), Ingredient(3, like=False)], ops(
        t, [Ingredient(1), Ingredient(3, like=False)]))
    assert [q for _c, q in more] == [True, True, False]
    assert {t.terms[c] for c, _q in more[:2]} == {"mood.dark", "structure.whodunit"}
    # Less names the less film's own terms: dark is the liked film's too.
    assert [(t.terms[c], q) for c, q in less] == [("themes.war", True), ("mood.grim", False)]


def test_the_derived_line_names_every_liked_film_even_one_read_by_us_alone():
    """Film 1's three quoted terms would fill the line; film 2, all our read, still gets its turn."""
    t = make({1: ["mood.dark!", "themes.heist!", "structure.whodunit!", "sensibility.tense!"],
              2: ["visual.pastel", "themes.family", "mood.cozy"], **filler(30)})
    recipe = [Ingredient(1), Ingredient(2)]
    more, _less = mix.derived(t, recipe, ops(t, recipe))
    assert len(more) == mix.DERIVED
    assert [q for _c, q in more] == [True, True, False], "quoted terms still lead the line"
    assert len({c for c, _q in more} & set(t.operand(2).cols.tolist())) == 1


def test_a_term_one_liked_film_quotes_reads_quoted_though_another_names_it_first():
    t = make({1: ["mood.dark", "register.plain"], 2: ["mood.dark!", "visual.pastel!"], **filler(30)})
    recipe = [Ingredient(1), Ingredient(2)]
    more, _less = mix.derived(t, recipe, ops(t, recipe))
    assert [(t.terms[c], q) for c, q in more] == [
        ("mood.dark", True), ("visual.pastel", True), ("register.plain", False)
    ]


def test_the_why_credits_each_group_to_one_film_and_names_a_less_films_own_terms():
    t = make({
        1: A, 2: B, 3: ["themes.war", "mood.grim", "mood.dark"],
        22: ["themes.heist!", "structure.whodunit", "mood.cozy", "mood.dark", "themes.war"],
        **filler(30),
    })
    recipe = [Ingredient(1), Ingredient(2, ("mood",)), Ingredient(3, like=False)]
    lines = {(i.title_id, i.like): [t.terms[c] for c, _q in named]
             for i, named in mix.why(t, recipe, ops(t, recipe), t.row_of[22])}
    assert set(lines[(1, True)]) == {"themes.heist", "structure.whodunit"}, "A's mood is lent away"
    assert lines[(2, True)] == ["mood.cozy"]
    assert lines[(3, False)] == ["themes.war"], "dark is A's own, so it is no 'but'"


def test_a_group_is_offered_at_two_terms_and_lists_quoted_terms_first():
    t = make({1: ["mood.dark", "mood.grim!", "sensibility.tense!", "visual.neon"]})
    sheet = {g: (offered, [t.terms[c] for c in q], [t.terms[c] for c in i])
             for g, offered, q, i in mix.group_terms(t, t.operand(1))}
    assert list(sheet) == list(mix.GROUPS)
    assert sheet["mood"][0] is True
    assert sorted(sheet["mood"][1]) == ["mood.grim", "sensibility.tense"]
    assert sheet["mood"][2] == ["mood.dark"]
    assert sheet["look"] == (False, [], ["visual.neon"])


def _twist_world(extra_liked=()):
    """Like A: 14 library films share A's two rare terms; 12 also carry L's pace, 5 L's sound."""
    films = {1: A, 2: B, 70: ["pacing.fast", "pacing.kinetic", "sound.drone", "sound.choral", "mood.warm"],
             **filler(60)}
    for i in range(14):
        films[100 + i] = ["themes.heist", "structure.whodunit"] + (["pacing.fast"] if i < 12 else []) \
            + (["sound.drone"] if i < 5 else []) + (["mood.cozy", "mood.warm"] if i < 11 else [])
    films.update(extra_liked)
    return make(films)


def twists(t, recipe, films, *, seed=0, rows=None) -> list[tuple[int, str, int]]:
    """One kind's twists: (film, group, library count)."""
    count = mix.twist_count(t, recipe, ops(t, recipe), t.owned if rows is None else rows)
    pairs = [("movie", op, g) for op, g in mix.twist_pairs(t, recipe, [t.operand(f) for f in films])]
    return [(f, g, n) for _k, f, g, n in mix.twists(pairs, {"movie": count}, seed=seed)]


def test_a_twist_keeps_ten_library_films_and_takes_no_claimed_group():
    t = _twist_world()
    recipe = [Ingredient(1)]
    picks = twists(t, recipe, [70, 2])
    assert {(f, g) for f, g, _n in picks} == {(70, "pace"), (2, "mood")}
    assert dict(((f, g), n) for f, g, n in picks)[(70, "pace")] == 12
    # Sound would leave five films; B's mood is taken once the recipe lends it.
    lent = [Ingredient(1), Ingredient(2, ("mood",))]
    assert [(f, g) for f, g, _n in twists(t, lent, [70])] == [(70, "pace")]
    narrowed = t.owned & ~np.isin(t.ids, [100, 101, 102])
    assert twists(t, recipe, [70], rows=narrowed) == []


def test_no_twist_at_four_films_or_two_lenders():
    t = _twist_world()
    four = [Ingredient(1), Ingredient(1000), Ingredient(1001), Ingredient(1002)]
    assert twists(t, four, [70]) == []
    two = [Ingredient(1), Ingredient(2, ("mood",)), Ingredient(100, ("storytelling",))]
    assert twists(t, two, [70]) == []


def test_the_shuffle_pages_through_every_twist_and_wraps():
    """Six films with two groups each: four pages of three, the first two naming each film once."""
    liked = {200 + i: ["pacing.fast", "pacing.kinetic", "mood.warm", "mood.cozy"] for i in range(6)}
    t = _twist_world(liked)
    recipe = [Ingredient(1)]
    pages = [twists(t, recipe, liked, seed=s) for s in range(5)]
    assert all(len(p) == mix.TWISTS for p in pages)
    assert twists(t, recipe, liked, seed=1) == pages[1], "a seed is one page, always the same"
    shown = [(f, g) for p in pages[:4] for f, g, _n in p]
    assert sorted(shown) == sorted((f, g) for f in liked for g in ("mood", "pace")), "each twist once"
    assert sorted(f for p in pages[:2] for f, _g, _n in p) == sorted(liked)
    assert pages[4] == pages[0], "the shuffle wraps round"


def test_a_page_short_of_three_wraps_and_three_or_fewer_always_show():
    liked = {200 + i: ["pacing.fast", "pacing.kinetic"] for i in range(4)}
    t = _twist_world(liked)
    recipe = [Ingredient(1)]
    first, second = twists(t, recipe, liked, seed=0), twists(t, recipe, liked, seed=1)
    assert second[0] not in first and second[1:] == first[:2]
    few = twists(t, recipe, [70, 2])
    assert len(few) == 2 and twists(t, recipe, [70, 2], seed=5) == few


def test_an_owned_flip_rederives_idf_without_new_terms():
    t = make({1: A, 21: ["themes.heist", "structure.whodunit"], **filler(30)})
    flipped = t.with_owned([1, 21], 2)
    assert flipped.cols is t.cols
    assert flipped.n_owned == 2 and int(flipped.owned.sum()) == 2
    assert flipped.idf[t.terms.index("register.plain")] == pytest.approx(math.log(2))
