# Spielplan backend + worker (same codebase, different entrypoint) — spec v2.1 §1, §2.
# CPU-only by construction: torch CPU wheels only, no CUDA, builds on a GPU-less VM.

# ---- stage 1: the SvelteKit PWA (static build, served by the backend) ----
FROM node:22-slim AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---- stage 2: python runtime ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_SYSTEM_PYTHON=1 \
    SPIELPLAN_STATIC_DIR=/app/static

# §2: "nightly pg_dump to /data/backups, rotation 14". The worker runs the binary itself rather
# than driving the `db` service, which would need the Docker socket inside a container whose
# stored connector secret is already admin-equivalent (§14.3) — and would have nothing to drive
# on an install whose DATABASE_URL points at a Postgres outside this compose file.
#
# Pinned to 16, from PGDG, because §1 pins the server to 16 and Debian trixie ships 17 in main:
# pg_dump refuses to dump a server newer than itself, and a dump taken by a newer client is one
# the household's own Postgres may not be able to read back. A client that is one major off in
# either direction is not a degraded backup, it is no backup.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl ca-certificates gnupg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg]" \
         "https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y --auto-remove gnupg \
 && rm -rf /var/lib/apt/lists/*

# Pinned. This line runs as root and installs the tool that installs everything else, so an
# unpinned `pip install uv` makes each rebuild an unreviewed supply-chain decision taken by
# whoever published most recently. [M4.7 sec-08]
RUN pip install uv==0.11.21

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
# torch (added at M2 for the Cold Tower) resolves from the CPU-only index, and it is
# backend/pyproject.toml's `[[tool.uv.index]]` that decides so — not a flag here. This line used
# to name an index strategy and an extra index instead, which asks for the highest version across
# both and so kept the image CPU-only by PEP 440 ordering luck. uv honours `tool.uv.sources` when
# it reads a pyproject as a requirements source, so this build, CI's jobs and a developer's
# editable install all get the same wheel. [M4.7 dd28]
RUN uv pip install --system -r pyproject.toml

COPY backend/spielplan ./spielplan
COPY backend/migrations ./migrations
COPY --from=frontend /app/build ./static

# The three console scripts `[project.scripts]` declares. Until this line the image installed
# `-r pyproject.toml` — pyproject as a *requirements source*, which resolves the dependencies and
# never the project — so `spielplan-migrate` did not exist in the container, and README's Recovery
# block could name neither `spielplan-secrets` nor `spielplan-movie-data` while both are the
# documented way out of a lost key and the only copy of the household's movie data.
#
# Editable, and `--no-deps`. Editable keeps /app the single import root, so
# `db/migrate.MIGRATIONS_DIR` (`parents[2] / "migrations"`) still resolves to the /app/migrations
# copied above — a non-editable wheel carries `packages = ["spielplan"]` and no migrations, so
# `spielplan-migrate` would find an empty directory and report nothing pending. `--no-deps`
# because the line above already resolved them, and a second resolution could move a pin.
# [M4.7 dd-deploy-image, spec-08, spec-07]
RUN uv pip install --system --no-deps -e .

# §14.3's threat model is that the stored connector credential is admin-equivalent, and §6's SPA
# fallback answers anonymous callers out of this same process — which ran as uid 0 in a container
# that also held every night's pg_dump until this milestone dropped that mount. uid 1000 is fixed
# rather than system-assigned because ./data is a set of host bind mounts: README's Recovery block
# and .github/workflows/ci.yml both chown to this exact number. [M4.7 sec-08]
# The group is created explicitly rather than left to `useradd`'s default, so the gid the host
# directories must carry is stated here and not inherited from /etc/login.defs.
#
# And /app stays root-owned. This RUN used to end `&& chown -R spielplan:spielplan /app` under
# the bind-mount sentence above, which is the wrong tree: ./data is mounted at /data and never
# under /app, so the chown reached nothing it named and instead handed the runtime user write
# access to /app/spielplan (the code), /app/migrations (the DDL every boot applies) and
# /app/static (the bundle the anonymous SPA fallback serves) — giving back, in one line, most of
# what the USER directive below is here to take away. `e2e/specs/07-boundaries.spec.js` records
# the traversal bug that once served a file straight out of that same handler; whether a reach
# like it can leave anything behind after a restart is exactly what the ownership of this tree
# decides.
#
# Nothing in the runtime needs it: every file this codebase writes is under `settings.data_dir`
# (/data), PYTHONDONTWRITEBYTECODE above removes the one write Python would attempt here on its
# own, and the editable install left its .pth in site-packages rather than in the source tree.
# Root-owned 0755 is readable and traversable by uid 1000, which is all `import` and `open` ask
# for. HOME is /app and is unwritable with it — a later dependency that insists on writing under
# $HOME gets a scratch path of its own, not a writable code tree. [M4.7 sec-08, cycle 2 finding 14]
RUN groupadd --system --gid 1000 spielplan \
 && useradd --system --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin spielplan

USER spielplan

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["uvicorn", "spielplan.app:app", "--host", "0.0.0.0", "--port", "8080"]
