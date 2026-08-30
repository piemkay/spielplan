"""Cold Tower placement — §8 stage 9, and §5.3's reconciliation of it. Spec v2.1 §4.3, §5.3, §10.

Four modules, split along the seams the spec puts budgets and rules on:

  * `contract`  — `feature_contract.json`, parsed. §4.3 calls it "the **exhaustive** definition
    of the tower's input", so this module owns every offset, width, column name and the frozen
    `text_scale`, and there is no second table of sizes anywhere to fall back to.
  * `features`  — the vector, built from the contract and the database. §8 stage 9: "builds
    vectors from this file and nothing else."
  * `tower`     — `cold_tower.pt`, loaded on the CPU (§1: "No GPU anywhere") and run forward to
    ê(t) and b̂(t).
  * `reconcile` — §5.3's sweep ("any owned title lacking a coordinate…") and §10's four-item
    rebuild set, of which this lens owns the fourth.

`contract` and the pure half of `features` import neither asyncpg nor torch: §5.3 puts a
per-title budget on placement, and a budget measured through Postgres is a measurement of
Postgres.
"""
