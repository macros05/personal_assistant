# CLAUDE.md — Personal Assistant Project Standards

## Architecture
- Agent loop lives in `agent.py` — never put business logic in `main.py`
- Each tool is a class extending `Tool` from `tools/base.py`
- Tools must implement: `name`, `description`, `schema` (property), `execute(**kwargs) -> dict`
- Max 6 agent rounds per request (`MAX_TOOL_ROUNDS = 6`) to prevent infinite loops
- `run_agent()` — full agent loop with history and tool calling (used by `/chat`, `/quick-action`)
- `run_once()` — one-shot call, no history, no tools (used by `/resumen` briefing)

## Code Quality
- Type hints on all function signatures and class methods
- One-line docstring on all public functions and classes
- `async/await` throughout — never block the event loop; use `asyncio.to_thread` for sync I/O
- Use `logging` module at module level (`log = logging.getLogger("module.name")`), never `print()`
- All secrets via `.env` — never hardcoded values in source files

## Adding New Tools
1. Create `tools/newtool.py` extending `Tool` from `tools/base.py`
2. Implement `name: str`, `description: str`, `schema` property, `async def execute(**kwargs)`
3. Register an instance in `tools/registry.py` → `_TOOLS` list
4. Add status label in `agent.py` → `_STATUS_LABELS` dict
5. Every `execute()` call is automatically logged to the `tool_calls` SQLite table via `agent.py`

## Flight Search Rules
- Always filter outbound flights by user work schedule (never suggest during work hours)
- **Outbound:** only Friday ≥14:30 (work ends 14:00 on Fridays), Saturday, or Sunday
- **Return:** only Sunday before 22:00, or Monday before 06:00
- Set `"optimal": true` on flights that match the schedule perfectly
- Flights failing the schedule filter are excluded; if none qualify, fall back to all with `"no_schedule_match": true`
- Sort all results by price ascending within each schedule bucket
- `origin` and `destination` are now dynamic parameters (default: AGP / WRO)
- `resolve_iata(code_or_city)` maps city names → IATA codes via `AIRPORT_CODES` dict
- Partner location: KRK (Kraków) during May → WRO (Wrocław) from June 3rd

## Airport Codes Reference (AIRPORT_CODES dict in tools/flights.py)
| City | IATA | Accepted city name inputs |
|------|------|--------------------------|
| Málaga | AGP | malaga, málaga |
| Madrid | MAD | madrid |
| Barcelona | BCN | barcelona |
| Wrocław | WRO | wroclaw, wrocław, breslavia, breslau |
| Kraków | KRK | krakow, kraków, cracovia, cracow |
| Warsaw | WAW | warsaw, varsovia, warszawa |
| Gdańsk | GDN | gdansk, gdańsk |
| London | LTN | london |
| Paris | CDG | paris |
| Amsterdam | AMS | amsterdam |
| Lisbon | LIS | lisbon, lisboa |

## Multi-Source Flight Search Pattern
All sources run in parallel via `asyncio.gather(return_exceptions=True)`. A failing source
logs a warning and is skipped — it never blocks results from other sources.

**Current sources in `tools/flights.py`:**
| Source | Function | Key required | Search type |
|--------|----------|--------------|-------------|
| Ryanair | `_fetch_ryanair()` | No | Date range |
| Vueling | `_fetch_vueling()` | No | Date range (best-effort; route may not exist) |
| Google Flights | `_fetch_serpapi()` | `SERPAPI_KEY` | Per schedule date, max 4/call (quota: 100/month free) |
| Skyscanner | `_fetch_skyscanner()` | `RAPIDAPI_KEY` | Single date (create session + poll) |

**Normalised fare shape** (all sources must return this):
```python
{
    "date":           "YYYY-MM-DD",
    "departure_time": "HH:MM",
    "price_eur":      float,
    "flight_number":  str,   # empty string if unknown
    "source":         str,   # "Ryanair" | "Vueling" | "Google Flights" | "Skyscanner"
}
```
The `"optimal"` key is **stamped after merge** by `_tag_schedule()` — source functions must NOT set it.

**Adding a new flight source:**
1. Write `async def _fetch_yourSource(origin, dest, ...) -> list[dict]` returning normalised fares
2. Add it to the `asyncio.gather(...)` call in `SearchFlightsTool.execute()`
3. Add its `(name, direction)` tuple to `source_names` in the same order
4. Add a colour class in `index.html` → `.flight-source-tag.yoursource { ... }`
5. Document it in this table

**Deduplication:** `_merge_and_dedup()` keys on `(date, departure_time)`. Same flight appearing
in multiple sources keeps the lower price and concatenates source names with ` / `.

**SerpAPI quota:** searches only schedule-friendly dates (`_MAX_SERPAPI_DATES = 4` per call).
At 1 call/day that's ~120 searches/month — slightly over free tier. Adjust `_MAX_SERPAPI_DATES`
or cache results if quota becomes an issue.

## Context
- Always read personal data from the SQLite `contexto` table — never hardcode values
- `proxima_visita_wroclaw` ISO date drives all countdown calculations in `context.py`
- `build_system_prompt()` in `context.py` is the single source of truth for the system prompt
- Context can be updated by the agent via `UpdateContextTool` (stored immediately to DB)

## Database Tables
- `messages` — conversation history (role, content, timestamp)
- `contexto` — personal key/value store (clave, valor, actualizado)
- `tool_calls` — execution log (tool_name, params, result, timestamp); written by `database.log_tool_call()`

## Frontend
- Dark theme, minimal — CSS variables in `:root` (`static/css/main.css`), no inline styles except layout overrides
- ES modules (`type="module"`) — no global variables; `index.html` contains only markup, `<link>`, and `<script type="module">`
- File structure:
  - `static/index.html` — markup only
  - `static/css/main.css` — all styles
  - `static/js/api.js` — all fetch/SSE calls; exports: `postChat`, `postQuickAction`, `getResumen`, `getVuelos`, `getCalendarEvents`, `getHistory`, `deleteHistory`, `getContexto`, `putContexto`, `deleteContexto`, `getAuthStatus`, `revokeCalendar`, `consumeSSE`
  - `static/js/ui.js` — DOM helpers; exports: `renderMessage`, `renderFlights`, `renderCalendar`, `renderContexto`, `renderTodayEvents`, `appendTyping`, `showToast`, `hideEmpty`, `scrollToBottom`, `setSendDisabled`, `autoResize`, `removeElement`, `updateTimestamp`, `escapeHtml`, `sourceTag`, `formatFlightDate`
  - `static/js/app.js` — imports from `api.js` and `ui.js`; owns all event listeners, app state (`isStreaming`, `calState`), and data loaders
- SSE streaming via `consumeSSE(response, bubble, onScroll)` in `api.js` — accepts a scroll callback to avoid DOM coupling
- GET streams (e.g. `/resumen`) use `startStreamingGet(fetchFn)`; POST streams use `startStreaming(fetchFn)` — both accept a fetch function, not a URL string
- No blocking UI during API calls — `isStreaming` flag gates all user actions (owned by `app.js`)
- Quick action buttons use `data-action` attributes; wired up in `app.js` via `querySelectorAll('[data-action]')`
- Mobile responsive: sidebar hides at `max-width: 660px`
- Sidebar sections load async on `DOMContentLoaded` — failures are silent (calendar/flights may not be available)

## Telegram Bot
- Implemented in `telegram_bot.py` as `TelegramBot` class; enabled only when `TELEGRAM_BOT_TOKEN` and `WEBHOOK_URL` are both set in `.env`
- Webhook is registered automatically on startup (`lifespan`) and deleted on shutdown
- Incoming updates hit `POST /telegram/webhook` → `TelegramBot.handle_update()` → `run_agent()` → reply
- `_collect_agent_response()` consumes the SSE async generator and extracts `text` events into a plain string
- Messages longer than 4000 chars are split into multiple Telegram messages
- `WEBHOOK_URL` must be a public HTTPS URL reachable by Telegram (use ngrok locally: `ngrok http 8000`)
- The bot handles `/start` and any free-form text; it shares the same agent, history, and tools as the web interface

## Routes Reference
| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Agent loop with tool calling |
| POST | `/quick-action/{action}` | Named prompt shortcuts |
| GET | `/resumen` | Morning briefing (no history, no tools) |
| GET | `/vuelos?days=30` | Flight search AGP→WRO + WRO→AGP |
| GET | `/calendar/events?days=7` | Direct calendar JSON for sidebar |
| POST | `/calendar/event` | Direct event creation |
| GET/PUT/DELETE | `/contexto` | Personal context CRUD |
| GET/DELETE | `/auth/google` | OAuth2 flow |
| POST | `/telegram/webhook` | Telegram Bot API webhook receiver |
