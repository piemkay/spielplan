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

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
# torch (added at M2 for the Cold Tower) resolves from the CPU-only index;
# nothing here may drag in a CUDA wheel.
RUN uv pip install --system --index-strategy unsafe-best-match \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r pyproject.toml

COPY backend/spielplan ./spielplan
COPY backend/migrations ./migrations
COPY --from=frontend /app/build ./static

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["uvicorn", "spielplan.app:app", "--host", "0.0.0.0", "--port", "8080"]
