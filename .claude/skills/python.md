---
name: python-project-hygiene
description: Baseline hygiene for Python projects — typing, structure, testing, tooling.
---

# Python project hygiene

## Layout

- Source under `src/<package>/` or a top-level package folder. Tests under `tests/`.
- Entry points go through `python -m <package>` or a console script in `pyproject.toml`. No ad-hoc scripts at the root.

## Typing

- Every public function has type hints on arguments and return.
- Prefer `list[int]` over `List[int]` (Python 3.9+).
- `from __future__ import annotations` at the top of every module — forward references for free.
- Use `TypedDict` for structured dicts you can't easily convert to dataclasses.

## Dataclasses / Pydantic

- Internal structured data → `@dataclass(frozen=True, slots=True)`.
- External input (API / config) → `pydantic.BaseModel` with explicit field types and validators.
- Don't mix them: one layer, one tool.

## Errors

- Raise custom exceptions that carry context. Never `raise Exception("...")`.
- Catch narrow — `except ValueError`, not bare `except`.
- Log with `logger.exception(...)` when re-raising is not desirable.

## Tooling

- `ruff` for lint + format. `mypy --strict` or `pyright` for types.
- `pytest` for tests, with fixtures in `conftest.py`. No `unittest.TestCase` unless stdlib-only.

## IO and async

- File IO uses `pathlib.Path`, not `os.path`.
- If a function does network IO, prefer async. Don't mix sync and async in the same call stack.
- Never `time.sleep` in async code — use `asyncio.sleep`.

## Things to avoid

- Mutable default arguments (`def f(x=[])`). Use `None` + initialize inside.
- `from x import *` in anything that isn't `__init__.py`.
- Wildcard `try/except` that swallows stack traces.
