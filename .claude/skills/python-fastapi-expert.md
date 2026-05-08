---
name: python-fastapi-expert
description: Guidance for adding/modifying FastAPI routes in this personal assistant. Covers the existing routing conventions, SSE streaming, and how new endpoints integrate with the agent loop.
priority: HIGH
---

# python-fastapi-expert

## When to use
- Adding a new route to `main.py` (see Routes Reference in CLAUDE.md for existing paths).
- Modifying request/response models.
- Wiring a new tool's output to a client-facing endpoint.

## Project-specific rules
- **No business logic in `main.py`** — routes are thin wrappers that call into `agent.py` (`run_agent` / `run_once`) or directly into `tools/*`. Keep handlers at ≤ 30 lines.
- **All handlers must be `async def`**. For sync I/O use `asyncio.to_thread(fn, ...)`.
- **Pydantic models** for non-trivial request bodies — declare near the top of `main.py`, one model per endpoint.
- **SSE streaming** — use the existing `StreamingResponse` + `text/event-stream` pattern already in `/chat` and `/quick-action/{action}`. Client helper is `consumeSSE()` in `static/js/api.js`.
- **Auth** — `/chat`, `/quick-action`, `/resumen`, `/vuelos`, `/calendar/*`, `/contexto`, `/flights*`, `/seo/*` are all behind the session-cookie middleware. Public routes are only `/login`, `/static/*`, and `/telegram/webhook`.
- **Errors** — raise `HTTPException(status_code, detail=...)`. Don't return error dicts with 200 status.
- **Logging** — `log = logging.getLogger("module.name")` at module top; never `print`.

## Integration checklist for a new endpoint
1. Define Pydantic model (if body needed).
2. Add route in `main.py` with decorator (`@app.post` / `@app.get`).
3. If the endpoint triggers agent behavior, route through `run_agent()`; if it's a pure data fetch, call the tool's public helper directly.
4. Add the route to the **Routes Reference** table in `CLAUDE.md`.
5. If frontend-facing, add a matching helper in `static/js/api.js`.
