---
name: google-cloud-api-integrator
description: How OAuth, token reuse, and scope management work for Google Calendar and Google Fit in this project. Read this before touching tools/calendar.py or tools/fitness.py.
priority: MEDIUM
---

# google-cloud-api-integrator

## Shared OAuth flow
- **Credentials:** `credentials.json` (downloaded from Google Cloud Console, installed-app type) at the repo root.
- **Tokens:** `token.json` stores the user's refresh token; created on first successful `/auth/google` round-trip.
- **Both Calendar and Fitness share the same token file** — scopes are a single combined list in `tools/calendar.SCOPES`.

## Scope list (`tools/calendar.py` → `SCOPES`)
```python
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/fitness.activity.read",
    "https://www.googleapis.com/auth/fitness.sleep.read",
    "https://www.googleapis.com/auth/fitness.heart_rate.read",
]
```

**When you add a scope:** the existing `token.json` no longer satisfies the new scope list. The user must re-consent via `/auth/google`. Delete `token.json` or surface a clear prompt in the UI.

## Adding a new Google API
1. Add required scope(s) to `SCOPES` in `tools/calendar.py`.
2. Import the API's discovery module (e.g., `googleapiclient.discovery.build("gmail", "v1", ...)`).
3. **All calls are sync** — wrap with `asyncio.to_thread` when calling from a coroutine.
4. Tell the user they need to re-auth (delete `token.json` and visit `/auth/google`).
5. Document the new integration in `CLAUDE.md`.

## Failure handling
- If `token.json` is missing or refresh fails, the tool should return `{"error": "not_authorized"}` rather than crashing. The frontend shows a "Connect Calendar" button based on `/auth/google` status.
- Google API quotas: Calendar is generous; Fit is tighter. Don't poll either in a background loop.
