# Build Site Brief Agent

A local address-driven data-center siting analyst for learning agent loops, real public-data tools, deterministic scoring, SQLite memory, and visible traces.

## Setup

1. Create and activate a virtual environment: `python -m venv .venv`
2. Install dependencies: `pip install -r requirements.txt`
3. Add keys to `.env`: `OPENAI_API_KEY`, `MAPBOX_TOKEN`, and optional `EIA_API_KEY`
4. Keep `zoning.txt` in the project root so the zoning RAG tool can read it at startup.

## Run

Start the app with:

```bash
uvicorn api:app --reload
```

Open `http://127.0.0.1:8000`, search an address, confirm pins, then ask the chat to brief, screen, compare, or diligence sites. The right panel shows every model and tool call in the explicit loop.

## Notes

FEMA flood, HIFLD substations, and USGS elevation use public no-key endpoints. EIA regional power context is best-effort and returns `unavailable` without `EIA_API_KEY`. Public APIs do not expose true interconnection capacity, so every result must flag utility or ISO queue verification for a human.
