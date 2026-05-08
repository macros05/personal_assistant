"""Telegram alert sender and SQLite log for stock opportunities."""
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
import httpx

from modules import predictions as predictions_mod

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
            "cycle_phase TEXT",
            "cyclicality TEXT",
            "iv_hv_ratio REAL",
            "put_call_oi REAL",
            "next_earnings_date TEXT",
            "interest_coverage REAL",
            "share_change_pct REAL",
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
    cycle = record.get("cycle") or {}
    options = record.get("options") or {}
    catalysts = record.get("catalysts") or {}
    quality = record.get("quality") or {}

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO analyses
                (ticker, name, price, intrinsic_value, margin_of_safety,
                 score, opportunity, recommendation, reason, catalyst, raw_payload,
                 confidence, flags, data_quality, intrinsic_method, peer_context,
                 cycle_phase, cyclicality, iv_hv_ratio, put_call_oi,
                 next_earnings_date, interest_coverage, share_change_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?)
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
                cycle.get("phase"),
                cycle.get("cyclicality"),
                options.get("iv_hv_ratio"),
                options.get("put_call_oi"),
                catalysts.get("next_earnings_date"),
                quality.get("interest_coverage"),
                quality.get("share_change_pct"),
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


async def previous_analysis(ticker: str, *, before_id: Optional[int] = None) -> Optional[dict]:
    """Return the previous (or last) analysis for `ticker` BEFORE the current one.

    Used to compute score/recommendation/price deltas in the alert footer.
    """
    await init_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        if before_id is not None:
            cur = await db.execute("""
                SELECT id, score, recommendation, price, created_at
                FROM analyses
                WHERE ticker = ? AND id < ?
                ORDER BY id DESC
                LIMIT 1
            """, (ticker, before_id))
        else:
            # Skip the most recent row (which is the one just inserted).
            cur = await db.execute("""
                SELECT id, score, recommendation, price, created_at
                FROM analyses
                WHERE ticker = ?
                ORDER BY id DESC
                LIMIT 1 OFFSET 1
            """, (ticker,))
        row = await cur.fetchone()
        await cur.close()
    if not row:
        return None
    return {
        "id":             row[0],
        "score":          row[1],
        "recommendation": row[2],
        "price":          row[3],
        "created_at":     row[4],
    }


async def previous_alert(ticker: str) -> Optional[dict]:
    """Return the latest already-sent alert (price + score) before now."""
    await init_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("""
            SELECT score, price, sent_at
            FROM alerts_sent
            WHERE ticker = ?
            ORDER BY sent_at DESC
            LIMIT 1
        """, (ticker,))
        row = await cur.fetchone()
        await cur.close()
    if not row:
        return None
    return {"score": row[0], "price": row[1], "sent_at": row[2]}


def format_history_line(prev_analysis: Optional[dict], prev_alert_row: Optional[dict],
                        *, current_score: int, current_rec: str,
                        current_price: Optional[float]) -> str:
    """1-line history footer: previous score, recommendation, and price drift."""
    parts: list[str] = []
    if prev_analysis:
        prev_s = prev_analysis.get("score")
        prev_r = prev_analysis.get("recommendation") or "?"
        if isinstance(prev_s, int):
            delta = current_score - prev_s
            parts.append(f"prev. score {prev_s}→{current_score} ({delta:+d})")
        if prev_r and prev_r.upper() != (current_rec or "").upper():
            parts.append(f"rec {prev_r}→{current_rec}")
    if prev_alert_row and isinstance(prev_alert_row.get("price"), (int, float)) \
            and isinstance(current_price, (int, float)) and prev_alert_row["price"] > 0:
        pct = (current_price / prev_alert_row["price"] - 1.0) * 100.0
        parts.append(f"vs alerta previa: {pct:+.1f}% en precio")
    if not parts:
        return "Histórico: primera vez que esta empresa se acerca al umbral."
    return "Histórico: " + " · ".join(parts)


async def record_alert(ticker: str, score: int, price: Optional[float]) -> None:
    await init_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts_sent (ticker, score, price) VALUES (?, ?, ?)",
            (ticker, score, price),
        )
        await db.commit()


def format_alert(analysis: dict, *, accuracy_line: str = "",
                 history_line: str = "", alpha_line: str = "") -> str:
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
    macro          = analysis.get("macro") or {}
    momentum       = analysis.get("momentum") or {}
    quality        = analysis.get("quality") or {}

    def fmt_money(v) -> str:
        return f"${v:.2f}" if isinstance(v, (int, float)) else "N/A"

    def fmt_pct(v) -> str:
        return f"{v:.1f}%" if isinstance(v, (int, float)) else "N/A"

    flag_line = f"⚑ Flags: {', '.join(flags)}\n" if flags else ""
    peer_line = f"Peers: {peers}\n" if peers else ""
    method_line = f"Método VI: {method}\n" if method else ""

    macro_line = ""
    if macro.get("fed_funds_rate") is not None or macro.get("ust_10y") is not None:
        macro_line = (
            f"Macro: Fed {fmt_pct(macro.get('fed_funds_rate'))}, "
            f"UST10y {fmt_pct(macro.get('ust_10y'))}, "
            f"ciclo {macro.get('cycle_phase', '?')}, "
            f"sesgo {macro.get('valuation_bias', '?')}\n"
        )

    momentum_line = ""
    if momentum.get("available"):
        momentum_line = (
            f"Momentum: {momentum.get('trend')}, "
            f"vs SMA200 {fmt_pct(momentum.get('sma200_ratio'))}, "
            f"90d {fmt_pct(momentum.get('change_90d'))}\n"
        )

    quality_line = ""
    if quality.get("available"):
        quality_line = (
            f"Calidad: FCF yield {fmt_pct(quality.get('fcf_yield'))}, "
            f"Deuda/EBITDA {quality.get('debt_to_ebitda'):.1f}x"
            if isinstance(quality.get('debt_to_ebitda'), (int, float))
            else f"Calidad: FCF yield {fmt_pct(quality.get('fcf_yield'))}"
        )
        if isinstance(quality.get("roic"), (int, float)) and isinstance(quality.get("wacc"), (int, float)):
            quality_line += f", ROIC {fmt_pct(quality.get('roic'))} vs WACC {fmt_pct(quality.get('wacc'))}"
        quality_line += "\n"

    cycle = analysis.get("cycle") or {}
    catalysts_data = analysis.get("catalysts") or {}
    options = analysis.get("options") or {}

    cycle_line = ""
    if cycle.get("available"):
        cycle_line = (f"Ciclo: {cycle.get('cyclicality')} en fase "
                      f"{cycle.get('phase')} (fit {cycle.get('phase_fit'):+d})\n")

    catalyst_extra_line = ""
    days_to_e = catalysts_data.get("days_to_earnings")
    if isinstance(days_to_e, int) and 0 <= days_to_e <= 21:
        catalyst_extra_line = f"Próximo earnings: en {days_to_e}d ({catalysts_data.get('next_earnings_date')})\n"

    options_line = ""
    if options.get("available") and options.get("iv_hv_ratio"):
        options_line = (f"Opciones: IV/HV {options['iv_hv_ratio']:.2f} | "
                        f"PCR(OI) {options.get('put_call_oi'):.2f}\n"
                        if isinstance(options.get("put_call_oi"), (int, float))
                        else f"Opciones: IV/HV {options['iv_hv_ratio']:.2f}\n")

    history_footer = f"{history_line}\n" if history_line else ""
    accuracy_footer = f"{accuracy_line}\n" if accuracy_line else ""
    alpha_footer    = f"{alpha_line}\n" if alpha_line else ""

    return (
        "📊 OPORTUNIDAD DETECTADA\n"
        f"Empresa: {name} ({ticker})\n"
        f"Precio actual: {fmt_money(price)}\n"
        f"Valor intrínseco: {fmt_money(intrinsic)}\n"
        f"Margen de seguridad: {fmt_pct(margin)}\n"
        f"Puntuación: {score}/100  |  Confianza: {confidence}\n"
        f"Recomendación: {recommendation}\n"
        f"{method_line}"
        f"{macro_line}"
        f"{cycle_line}"
        f"{momentum_line}"
        f"{quality_line}"
        f"{options_line}"
        f"Análisis: {reason}\n"
        f"{peer_line}"
        f"Catalizador: {catalyst}\n"
        f"{catalyst_extra_line}"
        f"{flag_line}"
        f"{history_footer}"
        f"{accuracy_footer}"
        f"{alpha_footer}"
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

    accuracy = await predictions_mod.model_accuracy()
    accuracy_line = predictions_mod.format_accuracy_line(accuracy)
    try:
        alpha = await predictions_mod.alpha_vs_spy(window=30)
        alpha_line = predictions_mod.format_alpha_line(alpha)
    except Exception as e:
        log.warning("alpha_vs_spy failed: %s", e)
        alpha_line = ""
    prev_a = await previous_analysis(ticker)
    prev_al = await previous_alert(ticker)
    history_line = format_history_line(
        prev_a, prev_al, current_score=score, current_rec=analysis.get("recommendation", ""),
        current_price=analysis.get("price"),
    )

    body = format_alert(
        analysis,
        accuracy_line=accuracy_line,
        alpha_line=alpha_line,
        history_line=history_line,
    )
    if await send_telegram(body):
        await record_alert(ticker, score, analysis.get("price"))
        await predictions_mod.record_prediction(analysis)
        log.info("Alert sent for %s (score=%d, margin=%.1f%%, conf=%s)",
                 ticker, score, margin, confidence)
        return True
    return False
