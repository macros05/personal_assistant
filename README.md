# Personal Assistant

A personal AI assistant built with FastAPI and Gemini 2.5 Flash, accessible via web interface and Telegram bot. Designed for daily use: flight tracking, Google Calendar, financial overview, and free-form chat with native tool calling.

## Features

- **Web SPA** — dark-theme chat interface with sidebar, SSE streaming, and quick-action buttons
- **Telegram bot** — same agent accessible via Telegram (webhook-based)
- **Gemini native function calling** — multi-round tool loop, up to 5 rounds per request
- **Multi-source flight search** — Ryanair, Vueling, Google Flights (SerpAPI), Skyscanner in parallel; work-schedule filtering; any IATA route
- **Google Calendar** — OAuth2 integration; today's events in sidebar; create events via chat
- **Personal context store** — SQLite key/value store editable from the UI and by the agent
- **Morning briefing** — auto-triggered 06:00–10:00; pulls calendar, weather (Open-Meteo), finances

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + uvicorn |
| AI | Google Gemini 2.5 Flash (`google-genai`) |
| Database | SQLite via `aiosqlite` |
| Calendar | Google Calendar API v3 (OAuth2) |
| Telegram | `python-telegram-bot` v20 (webhook) |
| HTTP client | `httpx` |
| Frontend | Vanilla JS ES modules, no framework |

## Project Structure

```
personal_assistant/
├── main.py               # FastAPI app, all routes, lifespan
├── agent.py              # Gemini agent loop with tool calling
├── context.py            # System prompt builder, default context, quick actions
├── database.py           # SQLite helpers (messages, contexto, tool_calls)
├── telegram_bot.py       # Telegram webhook handler
├── tools/
│   ├── base.py           # Abstract Tool base class
│   ├── registry.py       # Tool registry
│   ├── flights.py        # Multi-source flight search
│   ├── calendar.py       # Google Calendar tool + auth helpers
│   ├── finances.py       # Financial snapshot from context DB
│   └── context_tool.py   # UpdateContext tool
└── static/
    ├── index.html
    ├── css/main.css
    └── js/
        ├── app.js        # Event listeners, state, orchestration
        ├── api.js        # All fetch/SSE calls
        └── ui.js         # DOM renderers
```

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/macros05/personal_assistant.git
cd personal_assistant
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required
GEMINI_API_KEY=your_gemini_api_key       # console.cloud.google.com → Gemini API
GEMINI_MODEL=gemini-2.5-flash

# Telegram bot (optional)
TELEGRAM_BOT_TOKEN=your_bot_token        # @BotFather on Telegram → /newbot
WEBHOOK_URL=https://your-public-url.com  # ngrok or VPS

# Flight search — optional, improves results
SERPAPI_KEY=your_serpapi_key             # serpapi.com (100 free searches/month)
RAPIDAPI_KEY=your_rapidapi_key           # rapidapi.com → Skyscanner API
```

### 3. Google Calendar (optional)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → enable **Google Calendar API**
3. Create OAuth 2.0 credentials → download as `credentials.json` → place in project root
4. Start the app and visit `http://localhost:8000/auth/google` to authenticate

### 4. Run

```bash
source venv/bin/activate
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`

### Telegram webhook (local development)

```bash
# In a separate terminal
ngrok http 8000
# Copy the https URL → set as WEBHOOK_URL in .env
# Restart the server — webhook registers automatically on startup
```

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Agent loop with tool calling (SSE) |
| POST | `/quick-action/{action}` | Named prompt shortcuts (SSE) |
| GET | `/resumen` | Morning briefing — no history, no tools (SSE) |
| GET | `/vuelos?days=30` | Flight search AGP→WRO + WRO→AGP |
| GET | `/calendar/events?days=7` | Today's events for sidebar |
| POST | `/calendar/event` | Create calendar event |
| GET/PUT/DELETE | `/contexto` | Personal context CRUD |
| GET/DELETE | `/history` | Chat history |
| GET | `/auth/google` | Google OAuth2 flow |
| DELETE | `/auth/google` | Revoke calendar access |
| POST | `/telegram/webhook` | Telegram Bot API receiver |

## Available Tools

| Tool | Description |
|------|-------------|
| `search_flights` | Multi-source search between any airports (IATA codes or city names) |
| `get_calendar_events` | Fetch upcoming Google Calendar events |
| `create_calendar_event` | Create a new event |
| `get_finances` | Financial snapshot from personal context |
| `update_context` | Update a key in the personal context store |

## Flight Search

Searches Ryanair, Vueling, Google Flights, and Skyscanner in parallel. Results are deduplicated by `(date, departure_time)` and filtered by work schedule:

- **Outbound:** Friday ≥14:30, Saturday, or Sunday
- **Return:** Sunday before 22:00, or Monday before 06:00

Supports any IATA route. Accepts city names (`"Kraków"`, `"malaga"`) or codes (`"KRK"`, `"AGP"`).

## License

MIT
