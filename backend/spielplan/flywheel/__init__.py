"""§8.4's extraction flywheel: the queue of naming failures future LLM spend should fix.

Spec v2.1 §8.4, §6.6 Data (the extraction queue with its approve/spend controls), §6.4 (the
compositional search and explore frontier that feed it), all as amended by v2.1.3; decisions 163,
328, 329, 330, 344 and 440.

§8.4: "A queue of *naming failures* that future LLM spend should fix, fed automatically". Migration
0004 froze its four feeds in `flywheel_item`'s kind CHECK and nothing ever read or wrote the table
(plan §2.2). What is here now, and what each module answers:

  * `store` - the queue itself: the one writer of a row, the admin queue's read, and the two feeds
    whose producers are M6's. `flywheel_item.reason` is "shown verbatim in the admin queue"
    (`0004_dna.sql:163`), so the writer composes the sentence and no reader rephrases it.
  * `thin` - the thin-facet observation, the one feed with a producer at M5 (decision 329: a title
    whose extracted tier leaves a declared facet unnamed), made by the driver the moment a walk
    finishes stage 8 (decision 440). The enqueue is immediate and visible, never a nightly digest
    (proposal 135, adopted under decision 330): a row is readable the moment the call returns, and
    "queued just now" is its own `created_at`.

THREE FEEDS AND NOT FOUR. §8.4's third, titles whose "unnamed taste" residual share is high, is
struck (decision 328): nothing this repository can read defines the measure, and 0029 narrows the
CHECK to the three kinds left. A naming failure whose term vocabulary v1 does not carry is not
enqueued at all (decision 344): no batch can add a term, so a row whose only control is Spend would
invite spend that cannot help.

WHY THE FLYWHEEL IS POSSIBLE, WHICH THE SPEC NEVER STATES (plan §2.8). §8.4 ends "breadth being the
measured binding constraint (zero owned titles carry `themes.robots`)", and read carelessly that
says the vocabulary lacks the term and the flywheel must grow it - which decision 163 forbids, a
vocabulary change being a migration and never an import. `themes.robots` IS a vocabulary-v1 term:
the corpus bundle's `artifacts/dna_vocab/v1/vocab_v1_all.tsv` carries it on line 166 of 583 (582
terms and a header), split from `themes.artificial_intelligence` in the "gap-fill v1.1 2026-08-21"
pass - re-grepped on 2026-09-24 in the v20260828 bundle the M4.5 exit import used, since the bundle
is not in this repository. So "breadth" is coverage of EXISTING terms across titles, the sentence is
about the library and not the vocabulary, and nothing in this package grows the vocabulary: a batch
asks a title's sources again, under the terms v1 already has.

THIS FILE IMPORTS NOTHING, and that is load-bearing rather than tidy. `acquire/pipeline.py` imports
`flywheel.thin` for the stage-8 observation, and the launch - which makes titles due through
`acquire/actions.py`, which imports the pipeline - lives in this package too, so a package
`__init__` that imported its modules would close that loop at import time.
"""
