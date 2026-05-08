---
name: python-asyncio-expert
description: Async/await patterns used in this project. Covers parallel tool execution, the multi-source flight-search gather pattern, and rules for avoiding event-loop blocking.
priority: HIGH
---

# python-asyncio-expert

## Non-blocking rule
Nothing in a coroutine may block the event loop. Specifically:
- **No `requests` library** — use `httpx.AsyncClient` (already a dependency).
- **No `time.sleep`** — use `asyncio.sleep`.
- **Sync SQLite calls** (sqlite3 stdlib) must be wrapped: `await asyncio.to_thread(db.get_history)`. See `database.py` — all public helpers are sync and called via `to_thread` from `main.py`.
- **OAuth / Google API clients** are sync (`googleapiclient.discovery`). Always call them through `asyncio.to_thread`.

## Parallel pattern (`asyncio.gather`)
The multi-source flight search in `tools/flights.py` is the canonical pattern:

```python
results = await asyncio.gather(
    _fetch_ryanair(...),
    _fetch_vueling(...),
    _fetch_serpapi(...),
    _fetch_skyscanner(...),
    return_exceptions=True,   # a failing source must NOT abort the others
)
for name, result in zip(source_names, results):
    if isinstance(result, Exception):
        log.warning("%s failed: %s", name, result)
        continue
    fares.extend(result)
```

- `return_exceptions=True` is required for any parallel fan-out to external APIs.
- Log the failing source, don't swallow silently.
- Keep the `source_names` list in the **same order** as the gather args.

## Background tasks
`main.py` starts background loops in `lifespan`:
- `_flight_poll_loop()` — polls AviationStack every 10 min.
- Telegram webhook registration / de-registration.

Rules for adding a new background task:
1. Define `async def _my_loop(): while True: await asyncio.sleep(N); ...`.
2. Start it in `lifespan` with `task = asyncio.create_task(_my_loop())`.
3. On shutdown: `task.cancel()` followed by `await asyncio.gather(task, return_exceptions=True)`.

## Agent loop
`agent.py::run_agent()` runs at most `MAX_TOOL_ROUNDS = 6` rounds. Each round:
1. Call Gemini with history + tools.
2. If the model returns tool calls, execute them in parallel via `asyncio.gather`.
3. Feed tool outputs back as the next user turn.

**Do not** increase `MAX_TOOL_ROUNDS` without a justified case — it's the circuit breaker against infinite tool loops.
