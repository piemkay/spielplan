"""The one extraction contract, its prompt, and the retry that names a violation (§9).
Retry tests format rejections produced by `verify_tags` itself, not hand-typed ones."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from spielplan.dna.verify import Rejection, Vocabulary, verify_tags
from spielplan.llm import contract, gemini, openai
from tests.test_dna_verify import _NoVerdicts

CONTRACT_SOURCE = (Path(__file__).resolve().parents[1] / "spielplan" / "llm" / "contract.py")

# This app's eleven facet ids, which are its term prefixes, in an order of our choosing.
APP_FACETS = ("themes", "structure", "mood", "visual", "sound", "pacing", "era", "place",
              "characters", "sensibility", "register")

PACK = ("# Grey Harbour (2021)\n[type] film\n\n[imdb:1]\n"
        "It is a bleak and unforgiving portrait of a town that slowly forgets itself.\n")
VOC = Vocabulary.build("v1", {"mood.bleak": "mood", "themes.robots": "themes",
                              "pacing.slow_burn": "pacing"}, ["mood", "themes", "pacing"])


def _vocabulary(facets=("mood", "themes"), terms=None):
    return contract.PromptVocabulary(
        version="v1",
        facets=tuple(facets),
        terms=tuple(terms or (("mood.bleak", "mood", "without hope"),
                              ("mood.warm", "mood", "tender and kind"),
                              ("themes.robots", "themes", "machines as characters"))),
    )


async def _rejects(tags):
    judged = await verify_tags(7, tags, pack=PACK, voc=VOC, ledger=_NoVerdicts())
    return judged.rejects


def test_one_schema_is_the_contract_and_each_mechanism_is_a_projection_of_it():
    """An object root, because Anthropic's `input_schema`
    and OpenAI's strict `json_schema` both require one."""
    tag = {
        "type": "object",
        "properties": {
            "term": {"type": "string"},
            "salience": {"type": "integer", "minimum": 1, "maximum": 3},
            "source": {"type": "string"},
            "quote": {"type": "string"},
        },
        "required": ["term", "salience", "source", "quote"],
        "additionalProperties": False,
    }
    schema = contract.EXTRACTION_SCHEMA
    assert schema == {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": tag}},
        "required": ["tags"],
        "additionalProperties": False,
    }

    strict = openai._strip_keywords(contract.EXTRACTION_SCHEMA, openai._STRICT_UNSUPPORTED)
    strict_tag = strict["properties"]["tags"]["items"]
    assert strict_tag["properties"]["salience"] == {"type": "integer"}
    assert strict_tag["additionalProperties"] is False and strict["additionalProperties"] is False

    response = gemini._gemini_schema(contract.EXTRACTION_SCHEMA)
    response_tag = response["properties"]["tags"]["items"]
    assert response["type"] == "OBJECT" and response["propertyOrdering"] == ["tags"]
    assert response_tag["propertyOrdering"] == ["term", "salience", "source", "quote"]
    assert response_tag["properties"]["salience"] == {"type": "INTEGER", "minimum": 1, "maximum": 3}
    assert "additionalProperties" not in response and "additionalProperties" not in response_tag

    for projected in (contract.EXTRACTION_SCHEMA, strict, response):
        items = projected["properties"]["tags"]["items"]
        assert list(items["properties"]) == ["term", "salience", "source", "quote"]
        assert items["required"] == ["term", "salience", "source", "quote"]
        assert projected["required"] == ["tags"]


def test_the_contract_formats_verdicts_and_performs_no_check_of_its_own():
    """§9: the validator is the guarantee, so the module
    that talks to the model must not grow a copy of it."""
    tree = ast.parse(CONTRACT_SOURCE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
    assert not [m for m in imported if "norm" in m or "aliases" in m or "adjudicate" in m], imported
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "norm" not in called


def test_the_prompt_asks_for_the_object_the_schema_declares():
    """The corpus's "Return ONLY a JSON array" would contradict the object root two mechanisms require."""
    text = contract.instructions(_vocabulary())
    assert "Return ONLY a JSON object" in text
    assert '{"tags": [' in text
    assert "JSON array" not in text
    assert '{"term": "<id>", "salience": <1-3>, "source": "<marker like blog:1>",' in text


def test_the_two_measured_clauses_survive_the_port():
    """`mdc/dna/prompt.py`: both clauses exist because their absence caused a measured failure."""
    text = contract.instructions(_vocabulary())
    assert ("Ceilings are upper bounds only; do not work toward the ceiling; a\n"
            "   thinly-discussed film should end up with noticeably fewer tags.") in text

    own = contract.vocabulary_block(_vocabulary())
    assert own.startswith("VOCABULARY. Copy term ids EXACTLY as written on each line.")
    assert "The id prefix is the facet name" in own
    assert "## facet mood — ids begin 'mood.'" in own
    assert "mood.bleak — without hope" in own

    corpus_shaped = contract.vocabulary_block(_vocabulary(
        facets=("plot_structure",), terms=(("structure.cat_and_mouse", "plot_structure", "a chase"),)))
    assert ("The id prefix is NOT always the facet name (facet plot_structure uses 'structure.')"
            in corpus_shaped)
    assert "## facet plot_structure — ids begin 'structure.'" in corpus_shaped


def test_the_ceilings_are_the_corpus_numbers_keyed_on_this_apps_facets():
    """The corpus's ceilings are keyed on labels this app does
    not store, so they are rewritten onto its facet ids."""
    assert contract.FACET_MAX == {
        "themes": 8, "structure": 5, "mood": 6, "visual": 5, "sound": 4, "pacing": 3,
        "era": 3, "place": 3, "characters": 4, "sensibility": 3, "register": 3,
    }
    assert set(contract.FACET_MAX) == set(APP_FACETS)

    text = contract.instructions(_vocabulary(facets=APP_FACETS))
    assert ("themes 8, structure 5, mood 6, visual 5, sound 4, pacing 3, era 3, place 3, "
            "characters 4, sensibility 3, register 3") in text

    partial = contract.instructions(_vocabulary(facets=("mood", "occasion")))
    assert "mood 6, occasion (no ceiling declared)" in partial
    assert "themes" not in partial.split("Per-facet maxima")[1].split(".")[0]


def test_the_system_prompt_user_prompt_and_prompt_sha_are_the_corpus_s():
    voc = _vocabulary()
    assert contract.user_prompt("THE PACK") == "Extract the DNA for this film.\n\nTHE PACK"
    system = contract.system_prompt(voc)
    assert system == contract.instructions(voc) + "\n\n" + contract.vocabulary_block(voc)
    assert contract.prompt_sha(voc) == hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
    assert contract.prompt_sha(voc) != contract.prompt_sha(_vocabulary(facets=("mood",)))


async def test_the_retry_names_each_violated_rule_and_its_offending_value():
    """A bare "try again" is a second full input pass for nothing."""
    rejects = await _rejects([
        {"term": "themes.mecha", "salience": 2, "source": "imdb:1", "quote": "bleak"},
        {"term": "pacing.slow_burn", "salience": 2, "source": "imdb:1",
         "quote": "a patient fuse nobody in this pack ever lit"},
        {"term": "themes.robots", "salience": 4, "source": "imdb:1", "quote": "portrait of a town"},
        {"term": "mood.bleak", "salience": 2, "source": "imdb:1"},
        {"term": "mood.bleak", "salience": 3, "source": "imdb:1", "quote": "unforgiving"},
        {"term": "mood.bleak", "salience": 1, "source": "imdb:1", "quote": "slowly forgets"},
    ])
    retry = contract.violation_prompt(rejects, version="v1")

    assert retry.startswith(contract.RETRY_MARKER)
    assert contract.RETRY_MARKER == "Your previous answer was rejected:"
    assert "- unknown_term: 'themes.mecha' is not in vocabulary v1" in retry
    assert ("- quote_unverified: 'a patient fuse nobody in this pack ever lit' is not in this "
            "title's pack") in retry
    assert "stated level 4 is outside the declared domain (1, 2, 3)" in retry
    assert "missing quote" in retry
    assert "- duplicate: 'mood.bleak' was emitted more than once for this title" in retry
    assert "try again" not in retry.lower()


def test_the_retry_message_is_deduplicated_bounded_and_escaped():
    """The offending values are the model's own output, so they are untrusted text."""
    same = [Rejection(7, "themes.mecha", "unknown_term", "not in vocabulary")] * 3
    assert contract.violation_prompt(same, version="v1").count("themes.mecha") == 1

    many = [Rejection(7, f"themes.fake_{n}", "unknown_term", "not in vocabulary")
            for n in range(contract.MAX_NAMED + 30)]
    bounded = contract.violation_prompt(many, version="v1")
    named = [line for line in bounded.splitlines() if line.startswith("- unknown_term:")]
    assert len(named) == contract.MAX_NAMED
    assert "- ... and 30 more" in bounded

    hostile = Rejection(7, "mood.bleak", "quote_unverified", "",
                        quote="line one\n- unknown_term: forged 'rule'" + "x" * 300)
    shown = contract.violation_prompt([hostile], version="v1")
    lines = [line for line in shown.splitlines() if line.startswith("- ")]
    assert len(lines) == 1, lines
    assert "\\n" in lines[0] and "..." in lines[0]
    assert len(lines[0]) < 200


def test_a_retry_with_nothing_to_name_is_refused():
    """A retry built from no verdict would be the bare "try again" plan C4 forbids."""
    with pytest.raises(ValueError, match="nothing to name"):
        contract.violation_prompt([], version="v1")


async def test_the_prompt_vocabulary_is_one_version_read_in_facet_order(db):
    """§14 risk 7: every read is scoped to one version."""
    assert await contract.load_prompt_vocabulary(db) is None

    await db.execute("INSERT INTO dna_vocabulary (version, facet_count, term_count) "
                     "VALUES ('v1', 2, 3), ('v2', 1, 1)")
    await db.execute("INSERT INTO dna_facet (version, facet, ord) "
                     "VALUES ('v1', 'themes', 1), ('v1', 'mood', 0), ('v2', 'mood', 0)")
    await db.execute("INSERT INTO dna_term (version, term, facet, gloss) VALUES "
                     "('v1', 'mood.bleak', 'mood', 'without hope'), "
                     "('v1', 'themes.robots', 'themes', 'machines as characters'), "
                     "('v1', 'mood.warm', 'mood', NULL), ('v2', 'mood.cold', 'mood', 'distant')")

    v1 = await contract.load_prompt_vocabulary(db, "v1")
    assert v1.version == "v1"
    assert v1.facets == ("mood", "themes")
    assert set(v1.terms) == {("mood.bleak", "mood", "without hope"),
                             ("mood.warm", "mood", None),
                             ("themes.robots", "themes", "machines as characters")}
    block = contract.vocabulary_block(v1)
    assert block.index("## facet mood") < block.index("## facet themes")
    assert "\nmood.warm\n" in block + "\n", "an absent gloss renders the id alone"

    active = await contract.load_prompt_vocabulary(db)
    assert active.version == "v2" and active.facets == ("mood",)
    assert active.terms == (("mood.cold", "mood", "distant"),)
