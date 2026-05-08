# Add Flight Source Skill

User command: /add-flight-source <name>

## Normalised fare shape (MUST match exactly):

{
    "date": "YYYY-MM-DD",
    "departure_time": "HH:MM",
    "price": float,
    "currency": "EUR",
    "source": "<Name>",
    "url": "https://...",
    "optimal": bool  # set by _tag_schedule(), never manually
}

## 5-Step Contract

1. Add async _fetch_<name>() function in tools/flights.py
2. Add to source_names tuple (for dedup tracking)
3. Apply _tag_schedule() AFTER fetching, never inside the fetcher
4. Add to asyncio.gather() in execute()
5. Test: the dedup key is (date, departure_time) — never (date, price)
