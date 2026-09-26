"""The acquisition spine (§8): `rawstore`, `fetch`, `queue`, and the `pipeline` driver.

`/data/raw` is mounted on the worker only (decision 345). Nothing here imports `spielplan.api`.
Stage 1 mints new titles and never updates a bundle row (decision 162).
"""
