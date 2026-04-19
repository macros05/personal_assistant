"""Google Calendar: auth helpers (used by main.py routes) + agent tool classes."""
import asyncio
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tools.base import Tool

log = logging.getLogger("tools.calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = "http://localhost:8000/auth/callback"
TIMEZONE = "Europe/Madrid"

_BASE = Path(__file__).parent.parent          # project root
TOKEN_PATH = _BASE / "token.json"
CREDENTIALS_PATH = _BASE / "credentials.json"


class CalendarAuthRequired(Exception):
    pass


class CalendarCredentialsMissing(Exception):
    pass


# ── Auth helpers ──────────────────────────────────────────────────────────────

def credentials_file_exists() -> bool:
    return CREDENTIALS_PATH.exists()


def is_authenticated() -> bool:
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        return creds.valid or bool(creds.refresh_token)
    except Exception:
        return False


def _read_client_info() -> dict:
    raw = json.loads(CREDENTIALS_PATH.read_text())
    info = raw.get("web") or raw.get("installed") or {}
    return {
        "client_id":     info["client_id"],
        "client_secret": info["client_secret"],
        "token_uri":     info.get("token_uri", "https://oauth2.googleapis.com/token"),
        "auth_uri":      info.get("auth_uri",  "https://accounts.google.com/o/oauth2/auth"),
    }


def get_auth_url() -> str:
    if not credentials_file_exists():
        raise CalendarCredentialsMissing(
            "No se encontró credentials.json. Descárgalo de Google Cloud Console."
        )
    info = _read_client_info()
    params = urllib.parse.urlencode({
        "client_id":     info["client_id"],
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
    })
    return f"{info['auth_uri']}?{params}"


def exchange_code(code: str) -> None:
    """Exchange OAuth2 code for tokens and persist to token.json."""
    info = _read_client_info()
    payload = urllib.parse.urlencode({
        "client_id":     info["client_id"],
        "client_secret": info["client_secret"],
        "redirect_uri":  REDIRECT_URI,
        "code":          code,
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request(
        info["token_uri"], data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())
    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri=info["token_uri"],
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=SCOPES,
    )
    TOKEN_PATH.write_text(creds.to_json())


def revoke() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def _get_service():
    if not TOKEN_PATH.exists():
        raise CalendarAuthRequired("No autenticado.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    elif not creds.valid:
        raise CalendarAuthRequired("Token inválido, vuelve a autenticarte.")
    return build("calendar", "v3", credentials=creds)


# ── Sync implementations (called via asyncio.to_thread) ──────────────────────

def _fetch_events(days: int = 7) -> dict:
    days = max(1, min(int(days), 90))
    service = _get_service()
    now = datetime.utcnow()
    end = now + timedelta(days=days)
    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat() + "Z",
            timeMax=end.isoformat() + "Z",
            singleEvents=True,
            orderBy="startTime",
            maxResults=25,
        ).execute()
    except HttpError as e:
        return {"error": str(e), "events": []}
    items = result.get("items", [])
    events = [
        {
            "id":          item.get("id", ""),
            "title":       item.get("summary", "(Sin título)"),
            "start":       item["start"].get("dateTime", item["start"].get("date", "")),
            "end":         item["end"].get("dateTime", item["end"].get("date", "")),
            "location":    item.get("location", ""),
            "description": item.get("description", ""),
            "meet_link":   item.get("hangoutLink", ""),
        }
        for item in items
    ]
    return {"events": events, "count": len(events), "days_queried": days}


def _insert_event(
    title: str,
    date: str,
    time: Optional[str] = None,
    description: Optional[str] = None,
    duration_minutes: int = 60,
) -> dict:
    service = _get_service()
    if time:
        t = time.strip().replace("h", ":").replace("H", ":")
        if ":" not in t and len(t) == 4:
            t = f"{t[:2]}:{t[2:]}"
        elif ":" not in t and len(t) <= 2:
            t = f"{t.zfill(2)}:00"
        start_dt = datetime.fromisoformat(f"{date}T{t}")
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        start_body = {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE}
        end_body   = {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE}
    else:
        start_body = end_body = {"date": date}
    body: dict = {"summary": title, "start": start_body, "end": end_body}
    if description:
        body["description"] = description
    try:
        created = service.events().insert(calendarId="primary", body=body).execute()
    except HttpError as e:
        return {"error": str(e)}
    return {
        "id":        created.get("id"),
        "title":     created.get("summary"),
        "start":     created["start"].get("dateTime", created["start"].get("date")),
        "html_link": created.get("htmlLink", ""),
    }


# ── Tool classes ──────────────────────────────────────────────────────────────

class GetCalendarEventsTool(Tool):
    name = "get_calendar_events"
    description = (
        "Obtiene los próximos eventos del calendario de Google. "
        "Úsala cuando el usuario pregunte por agenda, citas, reuniones o eventos."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Días hacia adelante a consultar. Por defecto 7.",
                }
            },
        }

    async def execute(self, days: int = 7, **_) -> dict[str, Any]:
        if not is_authenticated():
            return {"error": "Google Calendar no autenticado.", "events": []}
        return await asyncio.to_thread(_fetch_events, days)


class CreateCalendarEventTool(Tool):
    name = "create_calendar_event"
    description = (
        "Crea un nuevo evento en Google Calendar. "
        "Úsala cuando el usuario quiera añadir cita, recordatorio o evento a su agenda."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title":            {"type": "string",  "description": "Título del evento."},
                "date":             {"type": "string",  "description": "Fecha en formato YYYY-MM-DD."},
                "time":             {"type": "string",  "description": "Hora inicio HH:MM (24h). Omitir si es todo el día."},
                "duration_minutes": {"type": "integer", "description": "Duración en minutos. Por defecto 60."},
                "description":      {"type": "string",  "description": "Notas adicionales (opcional)."},
            },
            "required": ["title", "date"],
        }

    async def execute(
        self,
        title: str,
        date: str,
        time: Optional[str] = None,
        duration_minutes: int = 60,
        description: Optional[str] = None,
        **_,
    ) -> dict[str, Any]:
        if not is_authenticated():
            return {"error": "Google Calendar no autenticado."}
        return await asyncio.to_thread(
            _insert_event, title, date, time, description, duration_minutes
        )
