import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from pydantic import BaseModel

from agent import run_agent, run_once
import tools.calendar as calendar
from context import build_system_prompt, DEFAULT_CONTEXT, QUICK_ACTIONS
from database import (
    clear_history, delete_contexto_key, get_all_contexto, get_all_messages,
    init_db, seed_contexto_if_empty, upsert_contexto,
)
from telegram_bot import TelegramBot

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY",      "")
GEMINI_MODEL        = os.getenv("GEMINI_MODEL",        "gemini-2.0-flash")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN",  "")
WEBHOOK_URL         = os.getenv("WEBHOOK_URL",         "")

client = genai.Client(api_key=GEMINI_API_KEY)

telegram: Optional[TelegramBot] = None

_WMO_CODES: dict[int, str] = {
    0: "Despejado", 1: "Mayormente despejado", 2: "Parcialmente nublado", 3: "Nublado",
    45: "Niebla", 48: "Niebla helada",
    51: "Llovizna ligera", 53: "Llovizna", 55: "Llovizna intensa",
    61: "Lluvia ligera", 63: "Lluvia", 65: "Lluvia intensa",
    71: "Nevada ligera", 73: "Nevada", 75: "Nevada intensa",
    80: "Chubascos", 81: "Chubascos moderados", 82: "Chubascos intensos",
    95: "Tormenta", 96: "Tormenta con granizo", 99: "Tormenta fuerte",
}

_QUICK_ACTION_LABELS: dict[str, str] = {
    "resumen":  "📋 Resumen del día",
    "week":     "📅 Mi semana",
    "finances": "💰 Mis finanzas",
    "wroclaw":  "✈️ Días hasta Wrocław",
    "focus":    "🎯 ¿En qué enfocarme hoy?",
}

_SSE_HEADERS = {
    "Cache-Control":    "no-cache",
    "X-Accel-Buffering": "no",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global telegram
    await init_db()
    await seed_contexto_if_empty(DEFAULT_CONTEXT)
    log.info("Gemini model: %s", GEMINI_MODEL)

    if TELEGRAM_BOT_TOKEN and WEBHOOK_URL:
        telegram = TelegramBot(token=TELEGRAM_BOT_TOKEN, webhook_url=WEBHOOK_URL)
        telegram.set_agent(client, GEMINI_MODEL)
        await telegram.setup_webhook()
    else:
        log.info("Telegram bot not configured (TELEGRAM_BOT_TOKEN or WEBHOOK_URL missing)")

    yield

    if telegram:
        await telegram.delete_webhook()


app = FastAPI(title="Asistente Personal de Marcos", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Pydantic models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str


class ContextoUpsert(BaseModel):
    clave: str
    valor: str


class CalendarEventCreate(BaseModel):
    title:            str
    date:             str
    time:             Optional[str] = None
    duration_minutes: int = 60
    description:      Optional[str] = None


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _fetch_weather_malaga() -> str:
    """Current weather for Málaga via Open-Meteo (free, no API key)."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude":  36.7201,
                    "longitude": -4.4203,
                    "current":   "temperature_2m,weathercode,windspeed_10m",
                    "timezone":  "Europe/Madrid",
                },
            )
            w    = r.json().get("current", {})
            code = w.get("weathercode", 0)
            temp = w.get("temperature_2m", "?")
            wind = w.get("windspeed_10m",  "?")
            return f"{_WMO_CODES.get(code, 'Desconocido')}, {temp}°C, viento {wind} km/h"
    except Exception:
        return "no disponible"


# ── Core routes ───────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/chat")
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío.")

    async def event_stream():
        async for chunk in run_agent(req.message, client, GEMINI_MODEL):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/quick-action/{action}")
async def quick_action(action: str):
    if action not in QUICK_ACTIONS:
        raise HTTPException(status_code=404, detail=f"Acción '{action}' no encontrada.")

    prompt = QUICK_ACTIONS[action]
    label  = _QUICK_ACTION_LABELS.get(action, action)

    async def event_stream():
        async for chunk in run_agent(prompt, client, GEMINI_MODEL, save_label=label):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/resumen")
async def resumen():
    """Morning briefing: gathers calendar + finances + weather, then streams Gemma response."""
    context_rows = await get_all_contexto()
    ctx          = {r["clave"]: r["valor"] for r in context_rows}

    # Calendar events for today
    events_today: list[dict] = []
    if calendar.is_authenticated():
        try:
            data = await asyncio.to_thread(calendar._fetch_events, 1)
            events_today = data.get("events", [])
        except Exception as e:
            log.warning("Calendar fetch failed for resumen: %s", e)

    # Weather
    weather = await _fetch_weather_malaga()

    # Days to Wrocław
    wroclaw_days = "desconocido"
    wroclaw_raw  = ctx.get("proxima_visita_wroclaw", "")
    if wroclaw_raw:
        try:
            visit        = date.fromisoformat(wroclaw_raw.strip().split()[0])
            wroclaw_days = str(max(0, (visit - date.today()).days))
        except Exception:
            pass

    if events_today:
        events_text = "\n".join(
            f"  • {e['title']}" + (f" a las {e['start'][11:16]}" if len(e.get("start", "")) > 10 else "")
            for e in events_today
        )
    else:
        events_text = "  Sin eventos hoy"

    system = build_system_prompt(context_rows)
    user_prompt = (
        f"Genera un resumen matutino breve y motivador para Marcos. Sé directo y práctico.\n\n"
        f"EVENTOS HOY:\n{events_text}\n\n"
        f"DÍAS HASTA WROCŁAW: {wroclaw_days} días (próxima visita: {wroclaw_raw})\n\n"
        f"SITUACIÓN FINANCIERA:\n"
        f"- Ahorros líquidos: {ctx.get('ahorros_liquidos', 'N/A')}\n"
        f"- Inversiones ETF: SP500 {ctx.get('inversion_sp500', 'N/A')}/mes\n"
        f"- Bitcoin: {ctx.get('inversion_bitcoin', 'N/A')}/semana\n"
        f"- Salario actual: {ctx.get('salario_actual', 'N/A')}\n\n"
        f"TIEMPO EN MÁLAGA: {weather}\n\n"
        f"Estructura: saludo breve + eventos + countdown Wrocław + nota financiera + "
        f"un foco concreto para hoy. Máximo 200 palabras."
    )

    async def event_stream():
        async for chunk in run_once(system, user_prompt, client, GEMINI_MODEL):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ── Telegram webhook ─────────────────────────────────────────────────────────

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not telegram:
        raise HTTPException(status_code=503, detail="Telegram bot not configured.")
    update_data = await request.json()
    await telegram.handle_update(update_data)
    return {"ok": True}


# ── Flight tracker ────────────────────────────────────────────────────────────

@app.get("/vuelos")
async def vuelos(days: int = 30):
    """Cheapest flights AGP → WRO via Ryanair public API."""
    from tools.flights import SearchFlightsTool
    result = await SearchFlightsTool().execute(days_ahead=days)
    if "error" in result and not result.get("flights"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ── Calendar endpoints ────────────────────────────────────────────────────────

@app.get("/calendar/events")
async def calendar_events(days: int = 7):
    """Direct calendar events for sidebar — does not use agent."""
    if not calendar.is_authenticated():
        return {"events": [], "count": 0, "authenticated": False}
    try:
        data = await asyncio.to_thread(calendar._fetch_events, days)
        data["authenticated"] = True
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/calendar/event")
async def create_calendar_event(body: CalendarEventCreate):
    """Direct event creation — does not use agent."""
    if not calendar.is_authenticated():
        raise HTTPException(status_code=401, detail="Google Calendar no autenticado.")
    result = await asyncio.to_thread(
        calendar._insert_event,
        body.title, body.date, body.time, body.description, body.duration_minutes,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Chat history ──────────────────────────────────────────────────────────────

@app.get("/history")
async def history():
    return {"messages": await get_all_messages()}


@app.delete("/history")
async def delete_history():
    await clear_history()
    return {"ok": True}


# ── Contexto CRUD ─────────────────────────────────────────────────────────────

@app.get("/contexto")
async def get_contexto():
    return {"contexto": await get_all_contexto()}


@app.put("/contexto")
async def put_contexto(body: ContextoUpsert):
    if not body.clave.strip():
        raise HTTPException(status_code=400, detail="La clave no puede estar vacía.")
    await upsert_contexto(body.clave.strip(), body.valor)
    return {"ok": True, "clave": body.clave, "valor": body.valor}


@app.delete("/contexto/{clave}")
async def remove_contexto(clave: str):
    await delete_contexto_key(clave)
    return {"ok": True, "clave": clave}


# ── Google Calendar auth routes ───────────────────────────────────────────────

@app.get("/auth/status")
async def auth_status():
    return {
        "authenticated":       calendar.is_authenticated(),
        "credentials_present": calendar.credentials_file_exists(),
    }


@app.get("/auth/google")
async def auth_google():
    try:
        url = calendar.get_auth_url()
        return RedirectResponse(url)
    except calendar.CalendarCredentialsMissing as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/auth/callback")
async def auth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="No se recibió código de autorización.")
    try:
        await asyncio.to_thread(calendar.exchange_code, code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al intercambiar código: {e}")
    return RedirectResponse("/?cal=ok")


@app.delete("/auth/google")
async def auth_revoke():
    calendar.revoke()
    return {"ok": True, "message": "Autenticación revocada."}
