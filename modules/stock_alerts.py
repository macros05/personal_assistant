"""Telegram alert sender and SQLite log for stock opportunities."""
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
import httpx

log = logging.getLogger("modules.stock_alerts")

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stock_alerts_log.db"
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

_DISCLAIMER = (
    "⚠️ EXPERIMENTO TÉCNICO — NO ES ASESORAMIENTO FINANCIERO.\n"
    "Salida de un modelo de lenguaje sobre datos públicos posiblemente incompletos o\n"
    "desactualizados. No constituye recomendación de inversión. Verifica de forma\n"
    "independiente antes de cualquier decisión. Decisión final siempre tuya."
)


async def init_db() -> None:
    """Create alert tables if they don't exist."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker           TEXT    NOT NULL,
                name             TEXT    NOT NULL,
                price            REAL,
                intrinsic_value  REAL,
                margin_of_safety REAL,
                score            INTEGER,
                opportunity      INTEGER,
                recommendation   TEXT,
                reason           TEXT,
                catalyst         TEXT,
                raw_payload      TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Lightweight, idempotent migration for the new columns. SQLite accepts
        # ADD COLUMN; we swallow "duplicate column" errors so re-runs are fine.
        for col_def in (
            "confidence TEXT",
            "flags TEXT",
            "data_quality TEXT",
            "intrinsic_method TEXT",
            "peer_context TEXT",
        ):
            try:
                await db.execute(f"ALTER TABLE analyses ADD COLUMN {col_def}")
            except aiosqlite.OperationalError:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts_sent (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker     TEXT NOT NULL,
                sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                score      INTEGER,
                price      REAL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_ticker_sent ON alerts_sent(ticker, sent_at)"
        )
        await db.commit()


async def log_analysis(record: dict) -> None:
    """Persist an analysis result regardless of whether it triggered an alert."""
    await init_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO analyses
                (ticker, name, price, intrinsic_value, margin_of_safety,
                 score, opportunity, recommendation, reason, catalyst, raw_payload,
                 confidence, flags, data_quality, intrinsic_method, peer_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("ticker", ""),
                record.get("name", ""),
                record.get("price"),
                record.get("intrinsic_value"),
                record.get("margin_of_safety"),
                record.get("score"),
                1 if record.get("opportunity") else 0,
                record.get("recommendation", ""),
                record.get("reason", ""),
                record.get("catalyst", ""),
                record.get("raw_payload", ""),
                record.get("confidence", ""),
                ",".join(record.get("flags", []) or []),
                record.get("data_quality", ""),
                record.get("intrinsic_method", ""),
                record.get("peer_context", ""),
            ),
        )
        await db.commit()


async def already_alerted_recently(ticker: str, days: int = 7) -> bool:
    """True if a Telegram alert for this ticker was sent within the last `days`."""
    await init_db()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM alerts_sent WHERE ticker = ? AND sent_at >= ? LIMIT 1",
            (ticker, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


async def record_alert(ticker: str, score: int, price: Optional[float]) -> None:
    await init_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts_sent (ticker, score, price) VALUES (?, ?, ?)",
            (ticker, score, price),
        )
        await db.commit()


def format_alert(analysis: dict) -> str:
    """Build the Spanish Telegram message body. Marcos's exact format."""
    name           = analysis.get("name", "")
    ticker         = analysis.get("ticker", "")
    price          = analysis.get("price")
    intrinsic      = analysis.get("intrinsic_value")
    margin         = analysis.get("margin_of_safety")
    score          = analysis.get("score", 0)
    recommendation = analysis.get("recommendation", "ESPERAR")
    confidence     = analysis.get("confidence", "LOW")
    flags          = analysis.get("flags") or []
    reason         = analysis.get("reason", "")
    catalyst       = analysis.get("catalyst") or "Sin titular destacado"
    peers          = analysis.get("peer_context") or ""
    method         = analysis.get("intrinsic_method") or ""

    def fmt_money(v) -> str:
        return f"${v:.2f}" if isinstance(v, (int, float)) else "N/A"

    def fmt_pct(v) -> str:
        return f"{v:.1f}%" if isinstance(v, (int, float)) else "N/A"

    flag_line = f"⚑ Flags: {', '.join(flags)}\n" if flags else ""
    peer_line = f"Peers: {peers}\n" if peers else ""
    method_line = f"Método VI: {method}\n" if method else ""

    return (
        "📊 OPORTUNIDAD DETECTADA\n"
        f"Empresa: {name} ({ticker})\n"
        f"Precio actual: {fmt_money(price)}\n"
        f"Valor intrínseco: {fmt_money(intrinsic)}\n"
        f"Margen de seguridad: {fmt_pct(margin)}\n"
        f"Puntuación: {score}/100  |  Confianza: {confidence}\n"
        f"Recomendación: {recommendation}\n"
        f"{method_line}"
        f"Análisis: {reason}\n"
        f"{peer_line}"
        f"Catalizador: {catalyst}\n"
        f"{flag_line}"
        f"{_DISCLAIMER}"
    )


async def send_telegram(text: str, *, client: Optional[httpx.AsyncClient] = None) -> bool:
    """Send a plain-text Telegram message to TELEGRAM_CHAT_ID. Returns True on success."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID")

    if not token or not chat_id:
        log.error("Telegram credentials missing — alert not sent")
        return False

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15.0)

    try:
        url = _TELEGRAM_API.format(token=token)
        r = await client.post(url, json={"chat_id": chat_id, "text": text})
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.error("Telegram send failed: %s", e)
        return False
    finally:
        if own_client:
            await client.aclose()


async def maybe_alert(
    analysis: dict,
    *,
    min_score: int = 70,
    min_margin: float = 15.0,
    cooldown_days: int = 7,
) -> bool:
    """Apply thresholds + cooldown and dispatch the Telegram alert. Returns True if sent."""
    if not analysis.get("opportunity"):
        return False
    score  = analysis.get("score") or 0
    margin = analysis.get("margin_of_safety") or 0
    ticker = analysis.get("ticker", "")
    confidence = (analysis.get("confidence") or "LOW").upper()
    flags  = analysis.get("flags") or []

    if score < min_score or margin < min_margin:
        return False
    # Block alerts when the data is too thin to trust the verdict, or when sanity
    # checks flagged the valuation as suspicious.
    if confidence == "LOW":
        log.info("Skipping %s — confidence is LOW", ticker)
        return False
    if "SUSPICIOUS_MEGACAP_UNDERVALUATION" in flags:
        log.info("Skipping %s — flagged as suspicious mega-cap undervaluation", ticker)
        return False
    if await already_alerted_recently(ticker, days=cooldown_days):
        log.info("Skipping %s — alerted within last %d days", ticker, cooldown_days)
        return False

    if await send_telegram(format_alert(analysis)):
        await record_alert(ticker, score, analysis.get("price"))
        log.info("Alert sent for %s (score=%d, margin=%.1f%%, conf=%s)",
                 ticker, score, margin, confidence)
        return True
    return False
