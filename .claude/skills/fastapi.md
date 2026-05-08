---
name: fastapi-conventions
description: Conventions and guardrails for FastAPI endpoints, dependencies, and async DB access.
---

# FastAPI conventions

Apply these rules whenever you touch a FastAPI route, dependency, or Pydantic model in this project.

## Route shape

- One router per resource, mounted from `main.py`.
- Keep handlers thin: validation → service call → response model. No business logic inline.
- Response models are **required** on every route (`response_model=...`). This doubles as OpenAPI docs and as a filter.
- Path parameters are typed (`user_id: int`, not `str`). Let FastAPI coerce.
- Status codes are explicit: `status_code=status.HTTP_201_CREATED`, not a magic integer.

## Pydantic

- Use `BaseModel` with explicit field types. No `dict[str, Any]` in request/response models.
- Split read/write models when they diverge (`UserCreate`, `UserRead`, `UserUpdate`).
- Use `Field(...)` for validation constraints (`min_length`, `ge`, `regex`) instead of runtime `if` checks.

## Dependencies

- Auth, DB sessions, and settings go through `Depends(...)` — never module-level state.
- Yield-based dependencies for anything needing teardown (DB sessions, file handles).

## Async / sync

- If the route is `async def`, the work inside must also be async. No blocking IO.
- For sync-only libraries (e.g. some ORMs), wrap them with `asyncio.to_thread(...)` or use the sync route — don't block the event loop.

## Errors

- Raise `HTTPException` for client-facing errors. Never return error dicts.
- Use a global exception handler for domain errors → consistent JSON shape.

## Testing

- Use `TestClient` for route tests; assert both status and response body.
- Never hit a real database in unit tests — use a fixture with a SQLite or a rolled-back session.
