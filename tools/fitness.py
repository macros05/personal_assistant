"""Google Fit: agent tool for retrieving today's fitness data (steps, sleep, heart rate, workouts, calories)."""
import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tools.base import Tool
from tools.calendar import SCOPES, TOKEN_PATH, is_authenticated

log = logging.getLogger("tools.fitness")

_SLEEP_ACTIVITY = 72
_NON_WORKOUT_ACTIVITIES = {0, 3, 4, 72, 109, 110, 111}  # unknown, still, sleep stages

_ACTIVITY_NAMES: dict[int, str] = {
    1: "Ciclismo", 7: "Caminar", 8: "Correr", 9: "Aeróbic",
    10: "Bádminton", 14: "Baloncesto", 17: "Bicicleta de montaña",
    20: "Boxeo", 25: "Circuito", 29: "Curling", 30: "Ciclismo",
    31: "Baile", 37: "Elíptica", 43: "Frisbee", 46: "Golf",
    47: "Gimnasia", 48: "Balonmano", 49: "Senderismo", 50: "Hockey",
    51: "Equitación", 55: "Kayak", 56: "Kettlebells", 57: "Kickboxing",
    60: "Artes marciales", 67: "Pilates", 69: "Racquetball",
    70: "Escalada", 71: "Remo", 73: "Fútbol", 75: "Squash",
    76: "Subir escaleras", 79: "Fuerza", 80: "Surf",
    81: "Natación", 82: "Piscina", 87: "Tenis", 88: "Cinta",
    91: "Voleibol", 95: "Pesas", 97: "Windsurf", 98: "Yoga",
    112: "Crossfit", 113: "HIIT", 116: "Entrenamiento por intervalos",
    169: "Caminata rápida",
}


def _get_fitness_service():
    """Build a Google Fit API client reusing the shared token.json."""
    if not TOKEN_PATH.exists():
        raise RuntimeError("No autenticado con Google.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    elif not creds.valid:
        raise RuntimeError("Token inválido, vuelve a autenticarte.")
    return build("fitness", "v1", credentials=creds)


def _activity_label(session: dict) -> str:
    name = (session.get("name") or "").strip()
    if name:
        return name
    activity = int(session.get("activityType", 0))
    return _ACTIVITY_NAMES.get(activity, f"Actividad {activity}")


def _fetch_fitness_data() -> dict[str, Any]:
    service = _get_fitness_service()

    now        = datetime.now().astimezone()
    start_dt   = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    sleep_from = start_dt - timedelta(hours=16)          # capture previous night
    start_ms   = int(start_dt.timestamp() * 1000)
    end_ms     = int(now.timestamp()      * 1000)

    def _aggregate(data_type: str) -> list[dict]:
        """Aggregate one dataType over today; returns [] if no datasource exists."""
        body = {
            "aggregateBy":     [{"dataTypeName": data_type}],
            "bucketByTime":    {"durationMillis": max(1, end_ms - start_ms)},
            "startTimeMillis": start_ms,
            "endTimeMillis":   end_ms,
        }
        try:
            r = service.users().dataset().aggregate(userId="me", body=body).execute()
        except HttpError as e:
            msg    = (e.content or b"").decode(errors="replace")
            status = getattr(e.resp, "status", None)
            if status == 403 and "has not been used in project" in msg:
                raise RuntimeError("api_disabled") from e
            if status in (401, 403):
                raise RuntimeError("needs_reauth") from e
            if "no default datasource" in msg:
                log.info("No datasource for %s — skipping", data_type)
                return []
            log.warning("Aggregate %s failed: %s", data_type, e)
            return []
        return r.get("bucket", [])

    try:
        step_buckets = _aggregate("com.google.step_count.delta")
        cal_buckets  = _aggregate("com.google.calories.expended")
        hr_buckets   = _aggregate("com.google.heart_rate.bpm")
    except RuntimeError as flag:
        if str(flag) == "api_disabled":
            return {
                "error":        "Fitness API deshabilitada en el proyecto de Google Cloud. Habilítala en https://console.developers.google.com/apis/api/fitness.googleapis.com",
                "api_disabled": True,
            }
        return {
            "error":        "Google Fit sin permisos. Vuelve a autenticar en /auth/google para conceder los scopes de fitness.",
            "needs_reauth": True,
        }

    steps    = 0
    calories = 0.0
    hr_vals: list[float] = []
    for bucket in step_buckets:
        for ds in bucket.get("dataset", []):
            for p in ds.get("point", []):
                for v in p.get("value", []):
                    steps += int(v.get("intVal") or 0)
    for bucket in cal_buckets:
        for ds in bucket.get("dataset", []):
            for p in ds.get("point", []):
                for v in p.get("value", []):
                    calories += float(v.get("fpVal") or 0)
    for bucket in hr_buckets:
        for ds in bucket.get("dataset", []):
            for p in ds.get("point", []):
                for v in p.get("value", []):
                    fp = v.get("fpVal")
                    if fp is not None:
                        hr_vals.append(float(fp))

    avg_hr = round(sum(hr_vals) / len(hr_vals), 1) if hr_vals else 0.0

    try:
        sessions = service.users().sessions().list(
            userId    = "me",
            startTime = sleep_from.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            endTime   = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        ).execute()
    except HttpError as e:
        log.warning("Google Fit sessions failed: %s", e)
        sessions = {"session": []}

    sleep_ms = 0
    workouts: list[dict] = []
    for s in sessions.get("session", []):
        activity    = int(s.get("activityType", 0))
        s_start     = int(s.get("startTimeMillis", 0))
        s_end       = int(s.get("endTimeMillis",   0))
        duration_ms = max(0, s_end - s_start)
        if activity == _SLEEP_ACTIVITY:
            sleep_ms += duration_ms
        elif activity not in _NON_WORKOUT_ACTIVITIES and s_end >= start_ms:
            workouts.append({
                "name":             _activity_label(s),
                "activity_type":    activity,
                "duration_minutes": round(duration_ms / 60_000),
                "start":            datetime.fromtimestamp(s_start / 1000).isoformat(timespec="minutes"),
            })

    return {
        "date":           start_dt.date().isoformat(),
        "steps":          steps,
        "sleep_hours":    round(sleep_ms / 3_600_000, 1),
        "avg_heart_rate": avg_hr,
        "workouts":       workouts,
        "calories":       round(calories),
    }


class GetFitnessDataTool(Tool):
    name        = "get_fitness_data"
    description = (
        "Get today's fitness data — steps, sleep, heart rate, workouts from Google Fit. "
        "Úsala cuando el usuario pregunte por actividad física, pasos, sueño o pulsaciones."
    )

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **_) -> dict[str, Any]:
        if not is_authenticated():
            return {"error": "Google Fit no autenticado."}
        return await asyncio.to_thread(_fetch_fitness_data)
