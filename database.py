import json
import aiosqlite
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "assistant.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                role      TEXT    NOT NULL,
                content   TEXT    NOT NULL,
                timestamp TEXT    NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contexto (
                clave      TEXT PRIMARY KEY,
                valor      TEXT NOT NULL,
                actualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tool_calls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT    NOT NULL,
                params    TEXT    NOT NULL,
                result    TEXT    NOT NULL,
                timestamp TEXT    NOT NULL
            )
        """)
        await db.commit()


async def seed_contexto_if_empty(defaults: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM contexto") as cursor:
            count = (await cursor.fetchone())[0]
        if count == 0:
            for clave, valor in defaults.items():
                await db.execute(
                    "INSERT INTO contexto (clave, valor) VALUES (?, ?)",
                    (clave, str(valor))
                )
            await db.commit()


# ── Messages ──────────────────────────────────────────────────────────────────

async def save_message(role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, datetime.utcnow().isoformat())
        )
        await db.commit()


async def get_recent_messages(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in reversed(rows)]


async def get_all_messages() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def clear_history():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages")
        await db.commit()


# ── Contexto ──────────────────────────────────────────────────────────────────

async def get_all_contexto() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT clave, valor, actualizado FROM contexto ORDER BY clave ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def upsert_contexto(clave: str, valor: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO contexto (clave, valor, actualizado) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, actualizado=CURRENT_TIMESTAMP",
            (clave, valor)
        )
        await db.commit()


async def delete_contexto_key(clave: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM contexto WHERE clave = ?", (clave,))
        await db.commit()


# ── Tool calls log ────────────────────────────────────────────────────────────

async def log_tool_call(tool_name: str, params: dict, result: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tool_calls (tool_name, params, result, timestamp) VALUES (?, ?, ?, ?)",
            (
                tool_name,
                json.dumps(params,  ensure_ascii=False),
                json.dumps(result,  ensure_ascii=False, default=str),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()
