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
3. Add its `(name

---

<!-- Auditoría: sugerencias automáticas -->

## Code Quality

## Code Quality
...
- All secrets via `.env` — never hardcoded values in source files. For Google API credentials, prefer environment variables or a secure secrets manager over `credentials.json` and `token.json`. If `credentials.json` is strictly necessary for OAuth flow, ensure it's handled securely and never committed to Git.
- Use `pytest` for unit and integration testing. Tests should reside in a `tests/` directory at the project root, mirroring the project structure (e.g., `tests/tools/test_flights.py`). Aim for high test coverage, especially for complex business logic in `agent.py` and `tools/`.
- Ensure proper error handling with custom exceptions where appropriate, logging errors, and returning meaningful responses to clients.

<!-- reason: Aclara la gestión de secretos para `credentials.json` y `token.json` en línea con la directriz de `.env`, introduce un estándar de testing (`pytest`) y la estructura para los tests, y refuerza la importancia del manejo de errores, mejorando la robustez y la seguridad. -->

## Deployment & Operations

## Deployment & Operations
- **Docker**: The `Dockerfile` should be optimized for production, using multi-stage builds to reduce image size and improve security. Ensure environment variables are correctly passed at runtime.
- **Monitoring**: Implement basic health checks (`/health`) and metrics collection (e.g., Prometheus/Grafana) for key services like the agent loop, tool calls, and API response times.
- **Database Migrations**: For `assistant.db` schema changes, use a tool like `Alembic` to manage migrations, ensuring smooth updates without data loss.

<!-- reason: Proporciona directrices esenciales para la fase de despliegue y operación, incluyendo optimización de Docker, monitoreo básico y gestión de migraciones de base de datos, aspectos clave para un proyecto en producción. -->

## Frontend Development

## Frontend Development (static/)
- **Vanilla JS**: Maintain a clean, modular structure for JavaScript files (ES modules) in `static/js/`. Avoid global variables.
- **CSS**: Use a consistent naming convention (e.g., BEM) and organize styles logically. Prioritize accessibility and responsiveness.
- **HTML**: Ensure semantic HTML5, accessibility (ARIA attributes), and proper meta tags.
- **Performance**: Optimize assets (minify JS/CSS, compress images) and lazy-load non-critical resources.

<!-- reason: Establece estándares de calidad y rendimiento para el desarrollo frontend, lo cual es crucial para la interfaz de usuario web del asistente, guiando a Claude en futuras modificaciones o adiciones a `static/`. -->

## Version Control

## Version Control
- All project files, including `CLAUDE.md`, must be managed under Git. Avoid manual backup files (e.g., `CLAUDE.md.backup-*`) in the repository.
- Follow conventional commit messages (e.g., `feat: add new feature`, `fix: bugfix`).

<!-- reason: Aborda directamente el problema de los archivos de backup de `CLAUDE.md` y establece una práctica clara de control de versiones, esencial para la colaboración y el historial del proyecto. -->
