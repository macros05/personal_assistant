---
name: docker-compose-expert
description: How to run and extend the project's Docker setup. Covers the existing Dockerfile, required env vars for container runs, and volume mounts for persistence.
priority: MEDIUM
---

# docker-compose-expert

## Current state
- `Dockerfile` exists at the repo root.
- `.dockerignore` excludes `.env`, `__pycache__/`, `*.db`, `node_modules/`, and backups.
- No `docker-compose.yml` yet — the app is a single container.

## Running the container
```bash
docker build -t personal-assistant .
docker run -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  personal-assistant
```

- **`data/` volume** must be mounted for SQLite persistence across container restarts.
- **Environment** is entirely driven by `.env`. See `.env.example` for required keys: `GEMINI_API_KEY`, `SESSION_SECRET`, plus optional integrations (`TELEGRAM_BOT_TOKEN`, `SERPAPI_KEY`, `RAPIDAPI_KEY`, `AVIATIONSTACK_KEY`, `INTERNAL_SEO_API_KEY`, `WEBHOOK_URL`).

## When you'd add docker-compose
Introduce a `docker-compose.yml` only if you add a second service that the app depends on (e.g., a postgres upgrade, a Redis cache, the SEO bot as a sibling service). Do not add compose for just the single app container — `docker run` suffices.

## Production-oriented Dockerfile improvements (if requested)
- Multi-stage build: builder stage installs deps into a venv, final stage copies the venv only.
- `--no-cache-dir` on pip installs.
- Run as non-root user: `RUN useradd -m app && USER app`.
- Expose health check: `HEALTHCHECK CMD curl -f http://localhost:8000/ || exit 1`.

Don't add these preemptively — they're useful for real deployment, not local dev.
