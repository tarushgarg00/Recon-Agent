# Recon

An address-driven data center siting analyst. You drop a pin on a map, ask a question,
and an AI agent pulls public data for flood risk, power-grid proximity, elevation,
zoning, and regional power context to evaluate the site for data center development.
Every step the agent takes is shown live, and the score is deterministic so each
recommendation traces back to a tool call and source.

Recon is a learning project for understanding agent loops, tool calling, RAG,
structured output, deterministic scoring, and human-in-the-loop review.

---

## How it works

```text
You pick a site and ask a question
        |
        v
The model decides which tools to call
        |
        v
Tools return public data and zoning chunks
        |
        v
Pure Python scores or ranks the site
        |
        v
Recon renders a screen, brief, risk register, or comparison artifact
```

The top of the app is a live workspace with Site Selection, Ask Recon, and Agent
Trace panels. The finished artifact appears below.

---

## Agent architecture

Recon keeps the agent loop explicit in `agent_core.py`:

1. The model reads the user message and confirmed sites.
2. The model decides whether to call tools or answer.
3. Tool results are appended back into the message history.
4. The loop repeats until the model is ready to finish.

The mode chips are hints only. They prefill editable prompts, but every mode still
routes through the same model/tool loop. Scoring and ranking are deterministic Python,
not model-generated numbers.

---

## Tools

| Tool | What it does | Source |
| --- | --- | --- |
| `get_site_data` | Fetches flood zone, nearest substation, elevation, and regional power context | FEMA NFHL, HIFLD/Open substations, USGS elevation, optional EIA |
| `search_zoning` | Retrieves relevant passages from the local zoning ordinance | `zoning.txt` |
| `score_site` | Computes a deterministic 0-100 screening score | Pure Python |
| `compare_sites` | Ranks scored sites by deterministic score | Pure Python |
| `save_site_brief` | Saves a completed brief to local memory | SQLite |
| `recall_site_briefs` | Recalls saved scored sites | SQLite |

True interconnection capacity is not available from these public APIs. Recon reports
grid proximity and always flags utility or ISO queue verification for a human.

---

## Main files

```text
agent_core.py          Main agent loop, tools, scoring, comparison, and memory logic.
api.py                 FastAPI app serving the frontend plus /chat and /chat/stream.
static/                Browser UI: map, chat, trace, and artifact rendering.
zoning.txt             Local ordinance text used by the zoning RAG tool.
00_raw_loop.py         Minimal raw OpenAI SDK tool-calling loop for teaching.
site_brief_agent.py    Smaller LangChain teaching version of the site-brief agent.
requirements.txt       Python dependencies.
.env.example           Environment variable template.
vercel.json            Vercel Python deployment configuration.
```

---

## Modes

- **Go / No-Go**: fast screen with verdict, score, and pass/fail checks.
- **Full diligence**: comprehensive findings, score breakdown, and next steps.
- **Risk deep dive**: risk register with severity and verification gaps.
- **Compare sites**: ranked comparison for 2+ confirmed sites with per-site evidence.

---

## Agent Trace

The right-hand panel is a live execution log. It shows model decisions, actual tool
calls, key arguments, durations, one-line result summaries, and expandable raw JSON.
For example:

```text
get_site_data(lat=33.45, lon=-112.07)
-> flood zone X, nearest substation 0.54 mi @ 69 kV, elevation 332.4 m

score_site(...)
-> score 89.6
```

This makes the recommendation auditable instead of a black box.

---

## Memory

Scored sites are saved in local SQLite (`sites.db`). Single-site artifacts show how a
new site compares to saved scored sites, such as "Ranks 1st of 4 saved scored sites."

---

## Setup

1. Create a virtual environment:

   ```bash
   python -m venv .venv
   ```

2. Activate it.

   Windows PowerShell:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and add:

   ```text
   OPENAI_API_KEY=...
   MAPBOX_TOKEN=...
   EIA_API_KEY=...
   ```

   `OPENAI_API_KEY` and `MAPBOX_TOKEN` are required. `EIA_API_KEY` is optional; without
   it, regional power context returns `unavailable`.

5. Keep `zoning.txt` in the project root so the zoning tool can read it at startup.

---

## Run locally

```bash
uvicorn api:app --reload
```

Open `http://127.0.0.1:8000`, search an address, confirm a pin, then ask a question
or choose a mode. Watch Agent Trace fill in as Recon works.

---
