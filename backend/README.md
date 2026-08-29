# spielplan (backend)

FastAPI + uvicorn. The worker runs the same codebase with a different entrypoint
(`python -m spielplan.worker`).

- `spielplan/core/` — config, secrets custody, auth primitives
- `spielplan/db/` — connection pool, migration runner, query modules
- `spielplan/importer/` — the artifact-bundle importer and its validation report (spec §10)
- `spielplan/api/` — HTTP routes
- `migrations/` — plain `.sql`, applied in filename order, checksummed

## Local development

```
uv venv .venv
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m pytest        # Windows
.venv/bin/python -m pytest            # POSIX
```

Tests that need Postgres read `TEST_DATABASE_URL` and skip when it is unset.
