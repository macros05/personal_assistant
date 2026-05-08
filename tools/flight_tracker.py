"""Flight tracker tool: saves a partner's flight to the DB and fetches initial status."""
import logging
import os
from typing import Any

import httpx

from database import add_flight
from tools.base import Tool

log = logging.getLogger("tools.flight_tracker")

_STATUS_MAP = {
    "scheduled": "SCHEDULED",
    "active":    "ACTIVE",
    "landed":    "LANDED",
    "cancelled": "CANCELLED",
    "incident":  "CANCELLED",
    "diverted":  "DELAYED",
}


async def fetch_flight_info(flight_number: str, date: str) -> dict | None:
    """Call AviationStack API for a live flight.

    The free plan only supports real-time flights (no flight_date filter).
    Returns None for future/completed flights that are not currently active.
    """
    key = os.getenv("AVIATIONSTACK_KEY", "")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "http://api.aviationstack.com/v1/flights",
                params={"access_key": key, "flight_iata": flight_number},
            )
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                log.warning("AviationStack API error for %s: %s", flight_number, body["error"])
                return None
            data = body.get("data", [])
            if not data:
                log.info("AviationStack: %s not currently active (flight may be future/past)", flight_number)
                return None
            d   = data[0]
            dep = d.get("departure", {}) or {}
            arr = d.get("arrival",   {}) or {}
            return {
                "origin":               dep.get("iata",      "") or "",
                "destination":          arr.get("iata",      "") or "",
                "scheduled_departure":  dep.get("scheduled", "") or "",
                "scheduled_arrival":    arr.get("scheduled", "") or "",
                "actual_departure":     dep.get("actual",    "") or "",
                "actual_arrival":       arr.get("actual",    "") or "",
                "status": _STATUS_MAP.get(d.get("flight_status", ""), "SCHEDULED"),
            }
    except Exception as e:
        log.warning("AviationStack error for %s on %s: %s", flight_number, date, e)
        return None


class TrackFlightTool(Tool):
    name = "track_flight"
    description = (
        "Track a specific flight number and save its status. "
        "Use when user mentions their partner is on a specific flight."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "flight_number": {
                    "type": "string",
                    "description": "Flight number e.g. FR1234, VY8012",
                },
                "date": {
                    "type": "string",
                    "description": "Flight date YYYY-MM-DD",
                },
                "person": {
                    "type": "string",
                    "description": "Who is on the flight, default: pareja",
                },
            },
            "required": ["flight_number", "date"],
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        flight_number = kwargs["flight_number"].upper().strip()
        date          = kwargs["date"].strip()
        person        = kwargs.get("person", "pareja")

        info = await fetch_flight_info(flight_number, date)

        flight_id = await add_flight(
            flight_number=flight_number,
            date=date,
            person=person,
            origin=info["origin"]               if info else "",
            destination=info["destination"]      if info else "",
            scheduled_departure=info["scheduled_departure"] if info else "",
            scheduled_arrival=info["scheduled_arrival"]     if info else "",
            actual_departure=info["actual_departure"]       if info else "",
            actual_arrival=info["actual_arrival"]           if info else "",
            status=info["status"]               if info else "SCHEDULED",
        )

        route = f"{info['origin']} → {info['destination']}" if info and info["origin"] else None
        status_desc = info["status"] if info else "SCHEDULED"
        live_note = (
            "Ruta y horarios se actualizarán automáticamente cuando el vuelo esté activo."
            if not info else ""
        )

        return {
            "ok":      True,
            "id":      flight_id,
            "message": (
                f"✈️ Vuelo {flight_number} del {date} para {person} añadido al seguimiento. "
                f"Estado actual: {status_desc}."
                + (f" Ruta: {route}." if route else "")
                + (f" {live_note}" if live_note else "")
            ),
        }
