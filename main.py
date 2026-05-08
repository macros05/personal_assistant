import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from agent import run_agent, run_once
import tools.calendar as calendar
from context import build_system_prompt, DEFAULT_CONTEXT, QUICK_ACTIONS
from database import (
    clear_history, delete_contexto_key, get_all_contexto, get_all_messages,
    init_db, seed_contexto_if_empty, upsert_contexto,
    add_flight, get_all_flights, get_active_flights, update_flight, delete_flight,
)
from telegram_bot import TelegramBot
from tools.fitness import _fetch_fitness_data
from tools.flight_tracker import fetch_flight_info

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
_tg_uid_raw         = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
TELEGRAM_ALLOWED_USER_ID: Optional[int] = int(_tg_uid_raw) if _tg_uid_raw else None
ADMIN_USERNAME      = os.getenv("ADMIN_USERNAME",      "")
ADMIN_PASSWORD      = os.getenv("ADMIN_PASSWORD",      "")
SECRET_KEY          = os.getenv("SECRET_KEY",          "changeme")
AVIATIONSTACK_KEY    = os.getenv("AVIATIONSTACK_KEY",    "")
INTERNAL_SEO_API_KEY = os.getenv("INTERNAL_SEO_API_KEY", "")

_SEO_BASE = os.getenv("SEO_BASE_URL", "http://localhost:8002")

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session")
_SESSION_COOKIE   = "session"
_SESSION_MAX_AGE  = 86_400       # 24 h default
_REMEMBER_MAX_AGE = 86_400 * 30  # 30 days

# Paths that bypass auth — prefix-matched
_PUBLIC_PREFIXES = ("/login", "/telegram/webhook", "/static", "/auth/callback")

client = genai.Client(api_key=GEMINI_API_KEY)

telegram:   Optional[TelegramBot]    = None
_poll_task: Optional[asyncio.Task]   = None

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


# ── Flight poll background task ───────────────────────────────────────────────

async def _notify_telegram_landed(person: str, destination: str) -> None:
    if telegram and TELEGRAM_ALLOWED_USER_ID:
        try:
            await telegram._bot.send_message(
                chat_id=TELEGRAM_ALLOWED_USER_ID,
                text=f"✈️ Tu {person} ha aterrizado en {destination}",
            )
            log.info("Telegram: notified landing in %s", destination)
        except Exception as e:
            log.error("Telegram landing notification failed: %s", e)


async def _update_active_flights() -> None:
    """Refresh AviationStack status for all non-final flights; fire landing alert if needed."""
    if not os.getenv("AVIATIONSTACK_KEY", ""):
        return
    flights = await get_active_flights()
    for f in flights:
        try:
            info = await fetch_flight_info(f["flight_number"], f["date"])
            if not info:
                continue
            await update_flight(
                f["id"],
                origin=info["origin"],
                destination=info["destination"],
                scheduled_departure=info["scheduled_departure"],
                scheduled_arrival=info["scheduled_arrival"],
                actual_departure=info["actual_departure"],
                actual_arrival=info["actual_arrival"],
                status=info["status"],
            )
            if f["status"] != "LANDED" and info["status"] == "LANDED":
                dest = info["destination"] or "destino desconocido"
                await _notify_telegram_landed(f["person"], dest)
        except Exception as e:
            log.warning("Failed to update flight %s %s: %s", f["flight_number"], f["date"], e)


async def _flight_poll_loop() -> None:
    log.info("Flight poll loop started (interval: 10 min)")
    while True:
        await asyncio.sleep(600)
        try:
            await _update_active_flights()
        except Exception as e:
            log.error("Flight poll error: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global telegram, _poll_task
    await init_db()
    await seed_contexto_if_empty(DEFAULT_CONTEXT)
    log.info("Gemini model: %s", GEMINI_MODEL)

    if TELEGRAM_BOT_TOKEN and WEBHOOK_URL:
        telegram = TelegramBot(token=TELEGRAM_BOT_TOKEN, webhook_url=WEBHOOK_URL, allowed_user_id=TELEGRAM_ALLOWED_USER_ID)
        telegram.set_agent(client, GEMINI_MODEL)
        # NOTE: do NOT call setup_webhook() here — seobot owns the bot via long-polling
        # and registering a webhook would race with its getUpdates loop. Free-form
        # messages reach this service via seobot's _forward_to_assistant forwarder.
        log.info("TelegramBot constructed; webhook handler ready at /telegram/webhook (seobot forwards updates)")
    else:
        log.info("Telegram bot not configured (TELEGRAM_BOT_TOKEN or WEBHOOK_URL missing)")

    if calendar.is_authenticated():
        log.info("Google Calendar: authenticated")
    else:
        log.warning("Google Calendar: NOT authenticated — visit /auth/google to re-link")

    _poll_task = asyncio.create_task(_flight_poll_loop())

    yield

    _poll_task.cancel()
    try:
        await _poll_task
    except asyncio.CancelledError:
        pass

    if telegram:
        await telegram.delete_webhook()


app = FastAPI(title="Asistente Personal de Marcos", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(STATIC_DIR))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=_REMEMBER_MAX_AGE)
        return True
    except (SignatureExpired, BadSignature):
        return False


# ── Auth middleware ───────────────────────────────────────────────────────────

@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    if not _is_authenticated(request):
        return RedirectResponse(f"/login?next={path}", status_code=302)
    return await call_next(request)


# ── Login / logout routes ─────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request, "error": None, "username": "",
    })


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request:  Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: str = Form(default=""),
    next:     str = Form(default="/"),
):
    user_ok = hmac.compare_digest(username.strip(), ADMIN_USERNAME)
    pass_ok = hmac.compare_digest(password, ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        return templates.TemplateResponse("login.html", {
            "request":  request,
            "error":    "Usuario o contraseña incorrectos.",
            "username": username,
        }, status_code=401)

    max_age = _REMEMBER_MAX_AGE if remember == "1" else _SESSION_MAX_AGE
    token = _serializer.dumps(username.strip())
    redirect_to = next if next.startswith("/") else "/"
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE, token,
        max_age=max_age, httponly=True, secure=True, samesite="lax",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(_SESSION_COOKIE)
    return response


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


class FlightTrackRequest(BaseModel):
    flight_number: str
    date:          str
    person:        str = "pareja"


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

    # Fitness (Google Fit) — skipped silently if not authed or scopes missing
    fitness: dict = {}
    if calendar.is_authenticated():
        try:
            data = await asyncio.to_thread(_fetch_fitness_data)
            if "error" not in data:
                fitness = data
        except Exception as e:
            log.warning("Fitness fetch failed for resumen: %s", e)

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

    if fitness:
        workouts = fitness.get("workouts") or []
        workouts_str = (
            ", ".join(f"{w['name']} ({w['duration_minutes']} min)" for w in workouts)
            if workouts else "ninguno"
        )
        fitness_text = (
            f"- Pasos: {fitness.get('steps', 0)}\n"
            f"- Sueño: {fitness.get('sleep_hours', 0)} h\n"
            f"- Frecuencia cardíaca media: {fitness.get('avg_heart_rate', 0)} bpm\n"
            f"- Calorías quemadas: {fitness.get('calories', 0)} kcal\n"
            f"- Entrenamientos: {workouts_str}"
        )
    else:
        fitness_text = "  (sin datos de Google Fit)"

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
        f"FITNESS (Google Fit, hoy):\n{fitness_text}\n\n"
        f"TIEMPO EN MÁLAGA: {weather}\n\n"
        f"Estructura: saludo breve + eventos + countdown Wrocław + nota financiera + "
        f"comentario sobre el fitness (sueño/pasos) + un foco concreto para hoy. Máximo 220 palabras."
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


# ── Tracked flights (Melanie) ─────────────────────────────────────────────────

@app.post("/flights/track")
async def track_flight_route(body: FlightTrackRequest):
    """Add a flight to the tracker, fetching initial status from AviationStack."""
    flight_number = body.flight_number.upper().strip()
    info = await fetch_flight_info(flight_number, body.date.strip())
    flight_id = await add_flight(
        flight_number=flight_number,
        date=body.date.strip(),
        person=body.person,
        origin=info["origin"]               if info else "",
        destination=info["destination"]      if info else "",
        scheduled_departure=info["scheduled_departure"] if info else "",
        scheduled_arrival=info["scheduled_arrival"]     if info else "",
        actual_departure=info["actual_departure"]       if info else "",
        actual_arrival=info["actual_arrival"]           if info else "",
        status=info["status"]               if info else "SCHEDULED",
    )
    return {"ok": True, "id": flight_id, "status": info["status"] if info else "SCHEDULED"}


@app.get("/flights")
async def list_tracked_flights():
    """Return all tracked flights."""
    return {"flights": await get_all_flights()}


@app.get("/flights/update")
async def refresh_tracked_flights():
    """Manually trigger an AviationStack refresh for all active flights."""
    await _update_active_flights()
    return {"flights": await get_all_flights(), "ok": True}


@app.delete("/flights/{flight_id}")
async def remove_tracked_flight(flight_id: int):
    await delete_flight(flight_id)
    return {"ok": True}


# ── Trading bot endpoints ─────────────────────────────────────────────────────

@app.get("/trading/status")
async def trading_status():
    """Return trading bot state, balance, PnL and circuit breaker status."""
    from tools.trading_bot import GetTradingStatusTool
    return await GetTradingStatusTool().execute()


# ── SEO Bot proxy ─────────────────────────────────────────────────────────────

@app.post("/seo/audit")
async def seo_audit(request: Request):
    """Proxy POST /seo/audit → SEO service /audit."""
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{_SEO_BASE}/audit", json=body,
                headers={"X-API-Key": INTERNAL_SEO_API_KEY},
            )
            return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"SEO service unavailable: {exc}")


@app.post("/seo/campaign")
async def seo_campaign(request: Request):
    """Proxy POST /seo/campaign → SEO service /pipeline/run. Cancels any running pipeline first."""
    body = await request.json()
    hdrs = {"X-API-Key": INTERNAL_SEO_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            # Cancel any stuck/running pipeline before starting a new one
            await c.post(f"{_SEO_BASE}/pipeline/cancel", headers=hdrs)
            r = await c.post(f"{_SEO_BASE}/pipeline/run", json=body, headers=hdrs)
            return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"SEO service unavailable: {exc}")


@app.post("/seo/cancel")
async def seo_pipeline_cancel():
    """Proxy POST /seo/cancel → SEO service /pipeline/cancel."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{_SEO_BASE}/pipeline/cancel",
                headers={"X-API-Key": INTERNAL_SEO_API_KEY},
            )
            return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"SEO service unavailable: {exc}")


@app.get("/seo/status")
async def seo_pipeline_status():
    """Proxy GET /seo/status → SEO service /pipeline/status."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{_SEO_BASE}/pipeline/status",
                headers={"X-API-Key": INTERNAL_SEO_API_KEY},
            )
            return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"SEO service unavailable: {exc}")


@app.get("/seo/prospects")
async def seo_prospects():
    """Proxy GET /seo/prospects → SEO service /outreach/status."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{_SEO_BASE}/outreach/status",
                headers={"X-API-Key": INTERNAL_SEO_API_KEY},
            )
            return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"SEO service unavailable: {exc}")


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
