import json
from typing import Optional
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_health (
                service       TEXT PRIMARY KEY,
                last_valid_at TEXT,
                last_alert_at TEXT,
                last_error    TEXT,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bridge_state (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS flights (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                flight_number       TEXT    NOT NULL,
                date                TEXT    NOT NULL,
                person              TEXT    NOT NULL DEFAULT 'pareja',
                origin              TEXT    DEFAULT '',
                destination         TEXT    DEFAULT '',
                scheduled_departure TEXT    DEFAULT '',
                scheduled_arrival   TEXT    DEFAULT '',
                actual_departure    TEXT    DEFAULT '',
                actual_arrival      TEXT    DEFAULT '',
                status              TEXT    NOT NULL DEFAULT 'SCHEDULED',
                last_checked        TIMESTAMP,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(flight_number, date)
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

# ── Flights ───────────────────────────────────────────────────────────────────

async def add_flight(
    flight_number: str, date: str, person: str,
    origin: str, destination: str,
    scheduled_departure: str, scheduled_arrival: str,
    actual_departure: str, actual_arrival: str,
    status: str,
) -> int:
    """Insert or update a tracked flight. Returns the row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO flights
                (flight_number, date, person, origin, destination,
                 scheduled_departure, scheduled_arrival,
                 actual_departure, actual_arrival, status,
                 last_checked, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(flight_number, date) DO UPDATE SET
                person=excluded.person,
                origin=excluded.origin,
                destination=excluded.destination,
                scheduled_departure=excluded.scheduled_departure,
                scheduled_arrival=excluded.scheduled_arrival,
                actual_departure=excluded.actual_departure,
                actual_arrival=excluded.actual_arrival,
                status=excluded.status,
                last_checked=CURRENT_TIMESTAMP
            """,
            (flight_number, date, person, origin, destination,
             scheduled_departure, scheduled_arrival,
             actual_departure, actual_arrival, status),
        )
        await db.commit()
        return cursor.lastrowid


async def get_all_flights() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM flights ORDER BY date ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_active_flights() -> list[dict]:
    """Return flights not yet landed or cancelled."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM flights WHERE status NOT IN ('LANDED','CANCELLED') ORDER BY date ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_flight(
    flight_id: int,
    origin: str,
    destination: str,
    scheduled_departure: str,
    scheduled_arrival: str,
    actual_departure: str,
    actual_arrival: str,
    status: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE flights SET
                origin=?, destination=?,
                scheduled_departure=?, scheduled_arrival=?,
                actual_departure=?, actual_arrival=?,
                status=?, last_checked=CURRENT_TIMESTAMP
               WHERE id=?""",
            (origin, destination,
             scheduled_departure, scheduled_arrival,
             actual_departure, actual_arrival,
             status, flight_id),
        )
        await db.commit()


async def delete_flight(flight_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM flights WHERE id=?", (flight_id,))
        await db.commit()


# ── OAuth health tracking ─────────────────────────────────────────────────────

async def oauth_record_valid(service: str) -> None:
    """Mark this service's OAuth as currently valid (clears error/alert)."""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO oauth_health (service, last_valid_at, last_alert_at, last_error, updated_at)
               VALUES (?, ?, NULL, NULL, CURRENT_TIMESTAMP)
               ON CONFLICT(service) DO UPDATE SET
                 last_valid_at=excluded.last_valid_at,
                 last_alert_at=NULL,
                 last_error=NULL,
                 updated_at=CURRENT_TIMESTAMP""",
            (service, now),
        )
        await db.commit()


async def oauth_record_failure(service: str, error: str) -> None:
    """Mark this service's OAuth as failing (preserves last_valid_at and last_alert_at)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO oauth_health (service, last_valid_at, last_alert_at, last_error, updated_at)
               VALUES (?, NULL, NULL, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(service) DO UPDATE SET
                 last_error=excluded.last_error,
                 updated_at=CURRENT_TIMESTAMP""",
            (service, error[:500]),
        )
        await db.commit()


async def oauth_record_alert_sent(service: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO oauth_health (service, last_alert_at, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(service) DO UPDATE SET
                 last_alert_at=excluded.last_alert_at,
                 updated_at=CURRENT_TIMESTAMP""",
            (service, now),
        )
        await db.commit()


async def oauth_get_state(service: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT service, last_valid_at, last_alert_at, last_error, updated_at FROM oauth_health WHERE service=?",
            (service,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


# ── Bridge / heartbeat state ──────────────────────────────────────────────────

async def bridge_set(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO bridge_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (key, value),
        )
        await db.commit()


async def bridge_get(key: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT key, value, updated_at FROM bridge_state WHERE key=?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


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
