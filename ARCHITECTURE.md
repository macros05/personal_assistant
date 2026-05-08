# Personal Assistant — Architecture

This document maps how the personal-assistant stack is wired so future
debugging never has to start from scratch. If something fails in production,
**read this first**, then jump to [TROUBLESHOOTING](#troubleshooting).

## Services

The whole stack runs under `/root/docker-compose.yml` on a single Hetzner VPS,
all on the docker network `marcos_network`.

| Container            | Image source                | Port      | Purpose                                                                                  |
|----------------------|-----------------------------|-----------|------------------------------------------------------------------------------------------|
| `personal_assistant` | `/root/personal_assistant`  | 8000      | FastAPI agent. Owns `/telegram/webhook`, Calendar OAuth, Gemini calls, dashboard.        |
| `seobot`             | `/root/ProyectoSEOBOT`      | 8002      | SEO outreach service. Owns the **Telegram long-polling** loop for `@macrosAssistant_bot`. |
| `trading_api`        | `/root/trading-bot`         | 8001      | Read-only trading bot status API.                                                        |
| `trading_bot`        | `/root/trading-bot`         | —         | Live trading process.                                                                    |
| `ai_dashboard`       | `/root/AIDashboard`         | 3000      | Next.js dashboard (separate domain).                                                     |

The bot, web UI and dashboard sit behind Nginx at `assistant.marcosmorales.dev`.

## Telegram bridge (the part that broke this week)

**One bot, two backends.** `@macrosAssistant_bot` is shared:

- `seobot` is the **only** process that calls Telegram's `getUpdates`. It owns
  long-polling. Approval commands (`/ok`, `/cancelar`, `/quitar_N`,
  `/telefono_N`, `/pending`, `/blacklist_*`, `/restart`) are handled here.
- `personal_assistant` does **not** poll and does **not** register a webhook
  with Telegram. It exposes `POST /telegram/webhook` for *internal* use only —
  called by `seobot._forward_to_assistant` for any text message that isn't a
  SEOBOT command.
- Every ~5 min `seobot._heartbeat_loop()` POSTs to
  `personal_assistant /telegram/heartbeat` so the bridge can be detected as
  alive even on slow chat days.

```
   ┌────────────┐         long-poll          ┌──────────┐
   │  Telegram  │ ◄──────────────────────────│  seobot  │
   └─────▲──────┘                            └────┬─────┘
         │                                        │ filters.TEXT & ~filters.COMMAND
         │  sendMessage replies                   │ → _forward_to_assistant
         │                                        ▼
         │                              ┌──────────────────────┐
         └──────────── 200 OK ──────────│ personal_assistant   │
                                        │  /telegram/webhook   │
                                        └──────────────────────┘
```

**Hard rule:** never call `bot.set_webhook()` from `personal_assistant`. It
races with seobot's `getUpdates(drop_pending_updates=True)` and the wrong side
will start dropping messages. If you ever need to switch to webhook mode, take
the polling out of seobot first.

## OAuth flow (Google Calendar + Fitness)

```
 user        seobot        personal_assistant       Google
  │            │                  │                   │
  │  /restart  │                  │                   │
  │ ───────►   │ -POST /restart-► │ os._exit(0)       │
  │            │                  │ (docker restarts) │
  │                                                   │
  user → /auth/google ─────────────► personal_assistant.get_auth_url()
                                          │
                                          ▼ 302 to Google consent screen
                                                   │
                                          ◄── code on /auth/callback ──
                                          │
                                          ▼ exchange_code(): persists token.json
```

`/auth/callback` lives in `_PUBLIC_PREFIXES` so Google's redirect can complete
even if the dashboard session has expired. **Do not remove that.**

`is_authenticated()` performs a *real* refresh check, not just a "file exists"
check. The result is recorded in the `oauth_health` SQLite table on every
startup and on every daily background probe.

## Resilience layers

| Layer                | Where                                 | Triggers                                                                      |
|----------------------|---------------------------------------|-------------------------------------------------------------------------------|
| OAuth daily probe    | `main._oauth_health_loop`             | Real `creds.refresh()`. On failure: Telegram alert with reauth URL, re-alert every 6 h. |
| Bridge watcher       | `main._bridge_health_loop`            | Reads `bridge_state` rows. Alerts if both heartbeat *and* last message > 15 min stale. |
| Gemini retry         | `gemini_retry.with_retry`             | 3 attempts at 1 s / 5 s / 30 s for transient errors (timeouts, 429, 5xx).     |
| Container restart    | `docker-compose.yml: restart: always` | If the process exits (e.g. `/restart`), it comes back automatically.          |
| Health endpoint      | `GET /health`                         | Public JSON with real probes (Calendar, Gemini, Telegram, bridge, DB).        |
| Status dashboard     | `GET /status`                         | HTML page polling /health every 30 s.                                          |
| Systemd timer        | `/etc/systemd/system/assistant-health.{service,timer}` | Curls /health every 5 min, logs to journald.    |

`/health` returns HTTP **503** when overall status is not `ok`, so anything
that watches HTTP status codes alone (uptime services, load balancers) can act.

## Logging

`structured_log.configure_logging()` is called at startup. Set `LOG_FORMAT=json`
in `personal_assistant/.env` to get JSON-per-line logs (with `correlation_id`).
Every HTTP request gets a 12-char correlation ID via `correlation_id_middleware`,
echoed back as the `X-Correlation-Id` response header so a client error can be
matched to server logs without timestamp guessing.

Logs are written to:
- stdout (Docker captures into `docker logs personal_assistant`)
- `/app/logs/assistant.log`, rotated at 100 MB, 7 backups (= 7 days at the
  current write rate).

## Restart from Telegram

`/restart` Telegram command (in seobot) → POST `/restart` on
`personal_assistant` with `X-Restart-Key` (HMAC of the bot token, derived
identically by both containers). Process calls `os._exit(0)`; Docker brings it
back in ~5 s. No SSH required.

## Where things live

- `main.py`             — FastAPI app: routes, middleware, lifespan, background loops.
- `agent.py`            — Gemini agent loop (now wrapped in `gemini_retry.with_retry`).
- `gemini_retry.py`     — Transient-error retry helper.
- `health.py`           — Probes + 30 s cache, used by `/health` and the loops.
- `structured_log.py`   — JSON formatter + correlation-id contextvar + rotation.
- `database.py`         — All SQLite queries, including `oauth_health` and `bridge_state`.
- `tools/calendar.py`   — Google Calendar OAuth helpers + tool classes.
- `tools/registry.py`   — Tool list shown to Gemini.
- `telegram_bot.py`     — `TelegramBot.handle_update` — receives updates from seobot.

In seobot:
- `outreach/telegram_bot.py` — long-polling app, `/restart`, heartbeat,
  `_forward_to_assistant`.

## Verifying the system after a change

```bash
# inside the host
curl -fsS http://127.0.0.1:8000/health | jq .
# expected: status=ok, all services ok

# from inside seobot, prove the bridge end-to-end
docker exec seobot python - <<'PY'
import httpx
r = httpx.post("http://personal_assistant:8000/telegram/webhook", json={
    "update_id": 9, "message": {"message_id": 1, "date": 0,
    "chat": {"id": 5372696572, "type": "private"},
    "from": {"id": 5372696572, "is_bot": False, "first_name": "Marcos"},
    "text": "ping"}}, timeout=30)
print(r.status_code, r.text)
PY
```

---

# TROUBLESHOOTING

## 1. "El asistente no responde a mensajes libres en Telegram"

This is exactly the failure that prompted this doc. Likely causes:

- **Calendar OAuth revoked.** Visit `/status`. If the `calendar` card is red,
  open `/auth/google` and re-link. The agent itself still answers — but every
  prompt that triggers a calendar tool will return an error string.
- **seobot is down or its forwarder was removed.** Check
  `docker logs seobot | grep forward_to_assistant`. You should see one log per
  free-form message (`received update_id=… text=…` then `OK`).
- **Webhook race.** `curl https://api.telegram.org/bot$TOKEN/getWebhookInfo`
  must return `url=""`. If a URL is set, something called `set_webhook` —
  remove that call and redeploy.
- **Bridge is alive but PA is unauthenticated.** Check `/health` from the
  host: `curl http://127.0.0.1:8000/health`. The `bridge` card shows the last
  heartbeat / last message age.

## 2. "Calendar dice 'no autenticado' aunque token.json existe"

The refresh_token has been rejected by Google (revoked, expired, scopes
removed). `is_authenticated()` actually attempts a refresh — if the log line
`calendar refresh_token rejected (...)` is in `docker logs personal_assistant`,
the only fix is to visit **`/auth/google`** while logged into the dashboard.
This generates a brand-new refresh token that Google will honor.

## 3. "/auth/callback redirige a /login y pierde el code"

Should not happen anymore: `/auth/callback` is in `_PUBLIC_PREFIXES`. If it
does, double-check `main._PUBLIC_PREFIXES` includes `/auth/callback` and that
no Nginx config is stripping query strings. The flow:

1. User clicks `/auth/google` → redirect to Google.
2. Google → `/auth/callback?code=...` (no session cookie required).
3. `exchange_code()` writes `token.json`.
4. Redirect to `/?cal=ok`.

## 4. "Gemini devuelve 503 / 429 ocasional"

Already handled by `gemini_retry.with_retry` (1 s / 5 s / 30 s). Look for
`gemini.run_agent: attempt N failed (...)` in the logs. If the third attempt
fails the agent returns the error to the user — that's intentional, we don't
want to retry forever.

## 5. "Un servicio se cayó y no se ha recuperado"

`docker-compose.yml` sets `restart: always` on every container. If a service
is reported `Up X seconds` repeatedly, it's crash-looping — `docker logs <name>`
will show the underlying error. The `/health` endpoint is HTTP 503 in that
window, which is loud enough to notice from anywhere.
