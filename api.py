import json
import os
from queue import Queue
from threading import Thread
from typing import Literal, Optional

from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_core import run_agent


load_dotenv(override=True)  # Loads local keys and lets .env win over empty shell values.


app = FastAPI(title="Recon")  # Creates the small local API server.


class SitePin(BaseModel):
    lat: float = Field(description="Confirmed site latitude.")
    lon: float = Field(description="Confirmed site longitude.")
    label: Optional[str] = Field(default=None, description="Optional user-facing site label.")


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"] = Field(description="Chat message role.")
    content: str = Field(description="Chat message content.")


class ChatRequest(BaseModel):
    message: str = Field(description="The user's freeform chat message.")
    sites: list[SitePin] = Field(default_factory=list, description="Confirmed map pins for this turn.")
    mode_hint: Literal["auto", "brief", "screen", "compare", "diligence"] = "auto"
    history: list[HistoryItem] = Field(default_factory=list, description="Prior chat turns from the UI.")


@app.exception_handler(Exception)
async def clean_error(_, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "reply": "The local server hit an error before the agent could finish.",
            "result": None,
            "needs_input": True,
            "trace": [],
            "loops": 0,
            "event_count": 0,
            "history_count": 0,
            "error": str(exc),
        },
    )  # Keeps API responses JSON-shaped instead of returning stack traces.


@app.get("/config")
def config():
    env_file = dotenv_values(".env")  # Reads .env fresh so token edits are picked up after reload.
    token = env_file.get("MAPBOX_TOKEN") or os.getenv("MAPBOX_TOKEN", "")
    return {"mapbox_token": token}  # Sends the browser a public Mapbox token without hardcoding it.


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        return run_agent(
            message=req.message,
            sites=[site.model_dump() for site in req.sites],
            mode_hint=req.mode_hint,
            history=[item.model_dump() for item in req.history],
        )  # Delegates all agent behavior to the explicit loop in agent_core.py.
    except Exception as exc:
        return JSONResponse(
            status_code=200,
            content={
                "reply": f"The agent could not complete this turn: {exc}",
                "result": None,
                "needs_input": True,
                "trace": [],
                "loops": 0,
                "event_count": 0,
                "history_count": 0,
            },
        )  # Returns clean JSON to the frontend even when a tool or model call fails.


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    q: Queue = Queue()

    def on_event(event):
        q.put({"type": "trace", "event": event})  # Sends each trace event to the response stream.

    def worker():
        try:
            data = run_agent(
                message=req.message,
                sites=[site.model_dump() for site in req.sites],
                mode_hint=req.mode_hint,
                history=[item.model_dump() for item in req.history],
                on_event=on_event,
            )
            q.put({"type": "done", "data": data})
        except Exception as exc:
            q.put(
                {
                    "type": "error",
                    "data": {
                        "reply": f"The agent could not complete this turn: {exc}",
                        "result": None,
                        "needs_input": True,
                        "trace": [],
                        "loops": 0,
                        "event_count": 0,
                        "history_count": 0,
                    },
                }
            )
        finally:
            q.put(None)

    def stream():
        Thread(target=worker, daemon=True).start()
        while True:
            item = q.get()
            if item is None:
                break
            yield json.dumps(item, default=str) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


app.mount("/", StaticFiles(directory="static", html=True, check_dir=False), name="static")  # Serves the vanilla frontend at root.
