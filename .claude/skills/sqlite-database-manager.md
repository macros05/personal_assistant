---
name: sqlite-database-manager
description: Schema and access patterns for the SQLite database in database.py. Covers existing tables, how to add a new one safely, and the sync-via-to_thread rule.
priority: MEDIUM
---

# sqlite-database-manager

## Current schema (`database.py`)
- **`messages`** — conversation history (`role`, `content`, `timestamp`).
- **`contexto`** — personal key/value store (`clave`, `valor`, `actualizado`).
- **`tool_calls`** — execution log (`tool_name`, `params`, `result`, `timestamp`); written by `database.log_tool_call()` from `agent.py`.
- **`flights`** — tracked partner flights; UNIQUE on `(flight_number, date)`.

## Access rules
- **All DB helpers in `database.py` are sync** (stdlib `sqlite3`) and must be called from coroutines via `asyncio.to_thread`:
  ```python
  history = await asyncio.to_thread(db.get_history, limit=50)
  ```
- **Never open a new connection per call in a hot path** — `database.py` uses a module-level helper that reuses a connection.
- **Parameterize all queries** — no f-string interpolation into SQL (`cursor.execute("... WHERE id = ?", (id,))`).

## Adding a new table
1. Add a `CREATE TABLE IF NOT EXISTS ...` statement to the schema init block in `database.py`.
2. Write typed sync helpers: `add_xxx`, `get_xxx`, `update_xxx`, `delete_xxx` — one line docstring each.
3. Include indexes for any column used in `WHERE` or `ORDER BY` at scale.
4. Document the table in the **Database Tables** section of `CLAUDE.md`.
5. Call the helpers via `asyncio.to_thread` from `main.py` / tools.

## No ORM
This project intentionally uses raw `sqlite3` — do not introduce SQLAlchemy, Tortoise, or similar. The schema is small and stable.

## Migrations
No migration tool is set up. For additive changes (new table, new column with default), rely on `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN`. For destructive changes, write a one-shot migration script in `scripts/` and run it manually.
