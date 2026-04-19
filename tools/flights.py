"""
Multi-source flight search: Ryanair, Vueling, SerpAPI (Google Flights), Skyscanner.

Supports any origin/destination pair via IATA codes. AIRPORT_CODES maps common city
names to IATA codes so Gemini can resolve natural-language input.

Sources run in parallel via asyncio.gather. Each returns a normalised list of fare
dicts; results are merged, deduplicated by (date, departure_time), and schedule-
filtered before being returned.

Source notes:
- Ryanair:       public fare API, no key required, range-based search.
- Vueling:       best-effort unofficial endpoint; many routes unavailable.
- SerpAPI:       wraps Google Flights; requires SERPAPI_KEY env var (100 req/mo free).
                 Searches per schedule-friendly date to conserve quota (max 4/call).
- Skyscanner:    RapidAPI Live Search; requires RAPIDAPI_KEY env var.
                 Searches a single target date (create session + one poll).
"""
import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import httpx

from tools.base import Tool

log = logging.getLogger("tools.flights")

# ── Airport code lookup ───────────────────────────────────────────────────────

AIRPORT_CODES: dict[str, str] = {
    # Spain
    "malaga":      "AGP", "málaga":    "AGP",
    "madrid":      "MAD",
    "barcelona":   "BCN",
    "sevilla":     "SVQ", "seville":   "SVQ",
    "valencia":    "VLC",
    "bilbao":      "BIO",
    "alicante":    "ALC",
    # Poland
    "wroclaw":     "WRO", "wrocław":   "WRO", "breslavia": "WRO", "breslau": "WRO",
    "krakow":      "KRK", "kraków":    "KRK", "cracovia":  "KRK", "cracow":  "KRK",
    "warsaw":      "WAW", "varsovia":  "WAW", "warszawa":  "WAW",
    "gdansk":      "GDN", "gdańsk":    "GDN",
    "katowice":    "KTW",
    # Other European
    "london":      "LTN",
    "paris":       "CDG",
    "amsterdam":   "AMS",
    "berlin":      "BER",
    "rome":        "FCO", "roma":      "FCO",
    "lisbon":      "LIS", "lisboa":    "LIS",
}


def resolve_iata(code_or_city: str) -> str:
    """Return uppercase IATA code, resolving common city names via AIRPORT_CODES."""
    return AIRPORT_CODES.get(code_or_city.strip().lower(), code_or_city.strip().upper())


# ── Config ────────────────────────────────────────────────────────────────────

_SERPAPI_KEY  = os.getenv("SERPAPI_KEY",  "")
_RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

_MAX_SERPAPI_DATES = 4          # cap per call to conserve 100/month free quota

PRICE_ALERT_THRESHOLD = 50.0

_RYANAIR_URL = "https://www.ryanair.com/api/farfnd/v4/oneWayFares"
_SERPAPI_URL = "https://serpapi.com/search.json"
_SKYSCANNER_CREATE_URL = "https://skyscanner-api.p.rapidapi.com/v3/flights/live/search/create"
_SKYSCANNER_POLL_URL   = "https://skyscanner-api.p.rapidapi.com/v3/flights/live/search/poll/{token}"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
}


# ── Schedule helpers (public so tests can import them) ────────────────────────

def _parse_dt(dep_date: str, dep_time: str) -> Optional[datetime]:
    """Parse date + HH:MM into datetime; return None on failure."""
    try:
        t = (dep_time or "00:00").strip()
        return datetime.fromisoformat(
            f"{dep_date}T{t}:00" if len(t) == 5 else f"{dep_date}T{t}"
        )
    except Exception:
        return None


def is_outbound_schedule_ok(dep_date: str, dep_time: str) -> bool:
    """True for Friday ≥14:30 (after work ends), Saturday, or Sunday."""
    dt = _parse_dt(dep_date, dep_time)
    if dt is None:
        return False
    wd = dt.weekday()          # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    if wd == 4:                # Friday: must be ≥14:30
        return dt.hour > 14 or (dt.hour == 14 and dt.minute >= 30)
    return wd in (5, 6)


def is_return_schedule_ok(dep_date: str, dep_time: str) -> bool:
    """True for Sunday before 22:00 or Monday before 06:00."""
    dt = _parse_dt(dep_date, dep_time)
    if dt is None:
        return False
    wd = dt.weekday()
    if wd == 6:
        return dt.hour < 22
    if wd == 0:
        return dt.hour < 6
    return False


# ── Utility helpers ───────────────────────────────────────────────────────────

def _schedule_dates(from_date: date, to_date: date, weekdays: tuple) -> list[str]:
    """ISO date strings for given weekdays within [from_date, to_date]."""
    dates, d = [], from_date
    while d <= to_date:
        if d.weekday() in weekdays:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def _merge_and_dedup(fares: list[dict]) -> list[dict]:
    """
    Deduplicate by (date, departure_time). When two sources report the same
    flight, keep the lower price and concatenate source names.
    """
    seen: dict[tuple, dict] = {}
    for f in fares:
        key = (f["date"], f["departure_time"])
        if key not in seen:
            seen[key] = dict(f)
        else:
            existing = seen[key]
            if f["price_eur"] < existing["price_eur"]:
                new_src = f"{f['source']} / {existing['source']}"
                seen[key] = {**f, "source": new_src}
            elif f["source"] not in existing["source"]:
                existing["source"] = f"{existing['source']} / {f['source']}"
    return sorted(seen.values(), key=lambda x: x["price_eur"])


def _tag_schedule(fares: list[dict], check: Callable[[str, str], bool]) -> list[dict]:
    """Stamp an 'optimal' bool onto each fare in-place."""
    for f in fares:
        f["optimal"] = check(f["date"], f["departure_time"])
    return fares


def _best_flights(fares: list[dict]) -> tuple[list[dict], bool]:
    """
    Return (display_list, schedule_filtered).
    Prefer optimal (schedule-friendly) flights; fall back to all with flag=False.
    """
    optimal = [f for f in fares if f.get("optimal")]
    if optimal:
        return optimal, True
    return fares, False


def _source_stats(fares: list[dict]) -> dict[str, int]:
    """Count raw fares per source, including merged source names."""
    stats: dict[str, int] = {}
    for f in fares:
        for src in f.get("source", "Unknown").split(" / "):
            stats[src.strip()] = stats.get(src.strip(), 0) + 1
    return stats


# ── Source: Ryanair ───────────────────────────────────────────────────────────

async def _fetch_ryanair(origin: str, dest: str, from_date: date, to_date: date) -> list[dict]:
    """Ryanair public cheapest fares API — range-based search."""
    params = {
        "departureAirportIataCode":  origin,
        "arrivalAirportIataCode":    dest,
        "outboundDepartureDateFrom": from_date.isoformat(),
        "outboundDepartureDateTo":   to_date.isoformat(),
    }
    headers = {**_BROWSER_HEADERS, "Referer": "https://www.ryanair.com/es/es/"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        resp = await client.get(_RYANAIR_URL, params=params)
        resp.raise_for_status()
        raw = resp.json().get("fares", [])

    fares: list[dict] = []
    for item in raw:
        out   = item.get("outbound", {})
        price = out.get("price", {}).get("value")
        dep   = out.get("departureDate", "")
        if price is None or not dep:
            continue
        fares.append({
            "date":           dep[:10],
            "departure_time": dep[11:16] if len(dep) > 10 else "",
            "price_eur":      round(float(price), 2),
            "flight_number":  out.get("flightNumber", ""),
            "source":         "Ryanair",
        })
    return fares


# ── Source: Vueling ───────────────────────────────────────────────────────────

async def _fetch_vueling(origin: str, dest: str, from_date: date, to_date: date) -> list[dict]:
    """
    Best-effort Vueling API attempt.
    AGP→WRO is unlikely to be served; fails silently if route unavailable.
    The unofficial endpoint may return 404 or structured error — both handled.
    """
    params = {
        "origin":        origin,
        "destination":   dest,
        "departureDate": from_date.isoformat(),
        "endDate":       to_date.isoformat(),
        "adults":        1,
        "currency":      "EUR",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.vueling.com/v1/flights",
            params=params,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    fares: list[dict] = []
    for flight in data.get("flights", []):
        price    = flight.get("price", {}).get("amount")
        dep_date = (flight.get("departureDate") or "")[:10]
        dep_time = (flight.get("departureTime") or "")[:5]
        if price is None or not dep_date:
            continue
        fares.append({
            "date":           dep_date,
            "departure_time": dep_time,
            "price_eur":      round(float(price), 2),
            "flight_number":  flight.get("flightNumber", ""),
            "source":         "Vueling",
        })
    return fares


# ── Source: SerpAPI (Google Flights) ─────────────────────────────────────────

async def _fetch_serpapi(origin: str, dest: str, dates: list[str]) -> list[dict]:
    """
    Google Flights data via SerpAPI. Searches each provided date individually.
    Requires SERPAPI_KEY env var. Capped at _MAX_SERPAPI_DATES to conserve quota.
    """
    if not _SERPAPI_KEY:
        return []

    fares: list[dict] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for d in dates[:_MAX_SERPAPI_DATES]:
            try:
                resp = await client.get(
                    _SERPAPI_URL,
                    params={
                        "engine":        "google_flights",
                        "departure_id":  origin,
                        "arrival_id":    dest,
                        "outbound_date": d,
                        "currency":      "EUR",
                        "hl":            "es",
                        "api_key":       _SERPAPI_KEY,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.warning("SerpAPI date %s failed: %s", d, e)
                continue

            for group in (data.get("best_flights", []) + data.get("other_flights", [])):
                price = group.get("price")
                segs  = group.get("flights", [])
                if not segs or price is None:
                    continue
                first_seg = segs[0]
                dep_raw   = first_seg.get("departure_airport", {}).get("time", "")
                # SerpAPI time format: "2026-05-01 06:30"
                if len(dep_raw) < 16:
                    continue
                fares.append({
                    "date":           dep_raw[:10],
                    "departure_time": dep_raw[11:16],
                    "price_eur":      round(float(price), 2),
                    "flight_number":  first_seg.get("flight_number", ""),
                    "source":         "Google Flights",
                })

    return fares


# ── Source: Skyscanner via RapidAPI ──────────────────────────────────────────

async def _fetch_skyscanner(origin: str, dest: str, target_date: str) -> list[dict]:
    """
    Skyscanner Live Search via RapidAPI (create session + single poll).
    Requires RAPIDAPI_KEY env var. Searches one date only.
    Price normalisation: Skyscanner v3 returns amounts; divide by 1000 if >10000
    to handle minor-unit encoding variations across API versions.
    """
    if not _RAPIDAPI_KEY:
        return []

    try:
        d = date.fromisoformat(target_date)
    except ValueError:
        return []

    rapid_headers = {
        "X-RapidAPI-Key":  _RAPIDAPI_KEY,
        "X-RapidAPI-Host": "skyscanner-api.p.rapidapi.com",
        "Content-Type":    "application/json",
    }
    body = {
        "query": {
            "market":    "ES",
            "locale":    "es-ES",
            "currency":  "EUR",
            "queryLegs": [{
                "originPlaceId":      {"iata": origin},
                "destinationPlaceId": {"iata": dest},
                "date":               {"year": d.year, "month": d.month, "day": d.day},
            }],
            "adults":     1,
            "cabinClass": "CABIN_CLASS_ECONOMY",
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        create = await client.post(
            _SKYSCANNER_CREATE_URL, json=body, headers=rapid_headers
        )
        create.raise_for_status()
        token = create.json().get("sessionToken", "")
        if not token:
            return []

        await asyncio.sleep(2)   # give Skyscanner time to assemble results

        poll = await client.get(
            _SKYSCANNER_POLL_URL.format(token=token),
            headers=rapid_headers,
        )
        poll.raise_for_status()
        data = poll.json()

    content     = data.get("content", {}).get("results", {})
    itineraries = content.get("itineraries", {})
    legs        = content.get("legs", {})

    fares: list[dict] = []
    for itin in itineraries.values():
        options   = itin.get("pricingOptions", [])
        price_raw = options[0].get("price", {}).get("amount", 0) if options else 0
        leg_ids   = itin.get("legIds", [])
        if not leg_ids or not price_raw:
            continue

        leg     = legs.get(leg_ids[0], {})
        dep_raw = leg.get("departureDateTime", {})
        if not dep_raw:
            continue

        dep_date_str = (
            f"{dep_raw.get('year', 0)}-"
            f"{dep_raw.get('month', 0):02d}-"
            f"{dep_raw.get('day', 0):02d}"
        )
        dep_time_str = f"{dep_raw.get('hour', 0):02d}:{dep_raw.get('minute', 0):02d}"

        # Normalise price — API encoding varies; divide by 1000 if implausibly large
        price_eur = float(price_raw)
        if price_eur > 10_000:
            price_eur /= 1000

        fares.append({
            "date":           dep_date_str,
            "departure_time": dep_time_str,
            "price_eur":      round(price_eur, 2),
            "flight_number":  "",
            "source":         "Skyscanner",
        })

    return sorted(fares, key=lambda x: x["price_eur"])[:8]


# ── Tool ──────────────────────────────────────────────────────────────────────

class SearchFlightsTool(Tool):
    name = "search_flights"
    description = (
        "Search cheapest flights between any two airports using Ryanair, Vueling, "
        "Google Flights and Skyscanner in parallel. "
        "Filters results by work schedule: outbound Friday ≥14:30, Saturday or Sunday; "
        "return Sunday before 22:00 or Monday before 06:00. "
        "Default route is AGP (Málaga) → WRO (Wrocław). "
        "Accepts IATA codes or common city names (e.g. 'Kraków', 'madrid')."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "origin": {
                    "type":        "string",
                    "description": "IATA origin airport code or city name. Default: AGP (Málaga).",
                },
                "destination": {
                    "type":        "string",
                    "description": "IATA destination airport code or city name. Default: WRO (Wrocław).",
                },
                "days_ahead": {
                    "type":        "integer",
                    "description": "How many days ahead to search. Default: 30.",
                },
            },
        }

    async def execute(
        self,
        origin:      str = "AGP",
        destination: str = "WRO",
        days_ahead:  int = 30,
        **_,
    ) -> dict[str, Any]:
        """Parallel multi-source search → merge → dedup → schedule filter."""
        origin      = resolve_iata(origin)
        destination = resolve_iata(destination)

        today    = date.today()
        end_date = today + timedelta(days=max(1, min(int(days_ahead), 90)))

        # Dates to query for per-date sources
        out_dates = _schedule_dates(today, end_date, weekdays=(4, 5, 6))   # Fri/Sat/Sun
        ret_dates = _schedule_dates(today, end_date, weekdays=(6, 0))      # Sun/Mon

        first_out = out_dates[0] if out_dates else today.isoformat()
        first_ret = ret_dates[0] if ret_dates else today.isoformat()

        # All 8 tasks in one gather — exceptions captured per task
        (
            ry_out, ry_ret,
            vu_out, vu_ret,
            sp_out, sp_ret,
            sk_out, sk_ret,
        ) = await asyncio.gather(
            _fetch_ryanair(origin, destination, today, end_date),
            _fetch_ryanair(destination, origin, today, end_date),
            _fetch_vueling(origin, destination, today, end_date),
            _fetch_vueling(destination, origin, today, end_date),
            _fetch_serpapi(origin, destination, out_dates),
            _fetch_serpapi(destination, origin, ret_dates),
            _fetch_skyscanner(origin, destination, first_out),
            _fetch_skyscanner(destination, origin, first_ret),
            return_exceptions=True,
        )

        # Collect results, log source failures
        source_names = [
            ("Ryanair", "out"),   ("Ryanair", "ret"),
            ("Vueling", "out"),   ("Vueling", "ret"),
            ("Google Flights", "out"), ("Google Flights", "ret"),
            ("Skyscanner", "out"), ("Skyscanner", "ret"),
        ]
        raw_out: list[dict] = []
        raw_ret: list[dict] = []
        source_errors: dict[str, str] = {}

        for (src, dirn), result in zip(source_names, [ry_out, ry_ret, vu_out, vu_ret, sp_out, sp_ret, sk_out, sk_ret]):
            if isinstance(result, Exception):
                log.warning("Source %s (%s) failed: %s", src, dirn, result)
                source_errors[f"{src} ({dirn})"] = str(result)
            else:
                target = raw_out if dirn == "out" else raw_ret
                target.extend(result)

        # Merge → dedup → tag schedule → select display set
        outbound_all = _tag_schedule(_merge_and_dedup(raw_out), is_outbound_schedule_ok)
        return_all   = _tag_schedule(_merge_and_dedup(raw_ret), is_return_schedule_ok)

        out_display, out_sched = _best_flights(outbound_all)
        ret_display, ret_sched = _best_flights(return_all)

        cheapest_out = out_display[0] if out_display else None
        cheapest_ret = ret_display[0] if ret_display else None

        return {
            # Backward-compat keys
            "flights":           out_display[:8],
            "cheapest":          cheapest_out,
            "alert_low_price":   cheapest_out["price_eur"] < PRICE_ALERT_THRESHOLD if cheapest_out else False,
            "no_schedule_match": not out_sched,
            # Structured outbound + return
            "outbound": {
                "flights":           out_display[:8],
                "cheapest":          cheapest_out,
                "schedule_filtered": out_sched,
            },
            "return_flights": {
                "flights":           ret_display[:8],
                "cheapest":          cheapest_ret,
                "schedule_filtered": ret_sched,
            },
            # Source metadata
            "source_stats":  _source_stats(raw_out + raw_ret),
            "source_errors": source_errors,
            "days_searched": days_ahead,
            "origin":        origin,
            "destination":   destination,
        }
