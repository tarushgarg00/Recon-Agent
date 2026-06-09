import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field


MODEL = "gpt-4o-mini"
DB_PATH = "sites.db"
HIFLD_SUBSTATIONS_URL = "https://services5.arcgis.com/HDRa0B57OVrv2E1q/ArcGIS/rest/services/Electric_Substations/FeatureServer/0/query"


load_dotenv()  # Loads local API keys from .env without hardcoding secrets.


ZONING_TEXT = open("zoning.txt", encoding="utf-8").read()  # Reads the user's real zoning text at startup.
splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120)  # Splits long zoning text for retrieval.
chunks = splitter.create_documents([ZONING_TEXT])  # Converts text chunks into LangChain documents.
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")  # Embeds chunks for semantic search.
zoning_store = InMemoryVectorStore.from_documents(chunks, embeddings)  # Stores zoning vectors in memory for this run.


class Reason(BaseModel):
    point: str = Field(description="A concise reason for the result.")
    grounded_in: str = Field(description="The exact fact, tool value, or zoning chunk supporting the point.")


class RankedSite(BaseModel):
    label: str = Field(description="Site label or rounded coordinate.")
    score: float = Field(description="Deterministic score from score_site.")
    reason: str = Field(description="Short deterministic ranking reason.")


class AgentResult(BaseModel):
    kind: str = Field(description="brief, screen, compare, diligence, question, or memo.")
    reply: str = Field(description="Natural-language answer for the user.")
    verdict: Optional[str] = Field(default=None, description="GO, CAUTION, or NO-GO when applicable.")
    score: Optional[float] = Field(default=None, description="Deterministic score when one site is evaluated.")
    reasons: list[Reason] = Field(default_factory=list, description="Grounded reasons for the result.")
    rankings: list[RankedSite] = Field(default_factory=list, description="Ranked sites for comparisons.")
    needs_human_review: str = Field(description="What a human must verify before relying on the result.")


SYSTEM_PROMPT = """
You are a data center siting analyst for auditable site screening.
Use real tools for facts and never invent data values.
Ask a clarifying question if you lack a coordinate or confirmed site.
Use get_site_data for public facts, search_zoning for legal rules, score_site for scores, compare_sites for rankings, and memory tools for saved sites.
Use plain English in user-facing text. Do not show raw JSON, field names, tool names, markdown headings, bullets, or numbered lists.
True interconnection capacity is not available from public APIs; always say a human must verify capacity with the utility or ISO queue.
Use score_site for numeric scores and compare_sites for rankings; never compute scores or ranks yourself.
When a coordinate already exists in memory, recall it instead of recomputing.
"""


def preview(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value  # Normalizes previews for trace display.
    return text[:limit] + ("..." if len(text) > limit else "")  # Keeps trace rows compact.


def key_for(lat: float, lon: float) -> str:
    return f"{round(lat, 4):.4f},{round(lon, 4):.4f}"  # Rounds coordinates so repeat sites match reliably.


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:  # Opens SQLite locally; the file is created at runtime.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefs (
                site_key TEXT PRIMARY KEY,
                lat REAL,
                lon REAL,
                label TEXT,
                brief_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )  # Creates the only memory table used by the app.


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_mi = 3958.8  # Mean Earth radius in miles for distance screening.
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return radius_mi * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def request_json(url: str, params: dict[str, Any], source: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        response = requests.get(url, params=params, timeout=10)  # Every public data call gets a timeout.
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, f"{source} unavailable: {exc}"  # External failures become data, not crashes.


def fetch_fema(lat: float, lon: float) -> dict[str, Any]:
    url = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY",
        "returnGeometry": "false",
        "f": "json",
    }
    data, error = request_json(url, params, "FEMA flood")
    if error:
        return {"flood_zone": "unavailable", "in_fema_flood_zone": "unavailable", "source": url, "fallback": error}
    features = data.get("features", []) if data else []  # ArcGIS returns matched flood features in this list.
    if not features:
        return {"flood_zone": "unavailable", "in_fema_flood_zone": "unavailable", "source": url, "fallback": "No FEMA NFHL feature intersected the point."}
    attrs = features[0].get("attributes", {})
    zone = attrs.get("FLD_ZONE") or "unavailable"
    is_flood = zone not in {"X", "AREA OF MINIMAL FLOOD HAZARD", "unavailable"}  # FEMA A/V-style zones are treated as flood concern.
    return {"flood_zone": zone, "zone_subtype": attrs.get("ZONE_SUBTY"), "in_fema_flood_zone": is_flood, "source": url}


def read_voltage(attrs: dict[str, Any]) -> Optional[float]:
    for name in ("MAX_VOLT", "MAXVOLT", "VOLTAGE", "VOLTAGE_KV", "KV", "SUB_1_VOLT"):
        value = attrs.get(name)
        try:
            return float(value) if value not in (None, "", "NOT AVAILABLE") else None
        except (TypeError, ValueError):
            continue
    return None


def fetch_hifld(lat: float, lon: float) -> dict[str, Any]:
    features = fetch_hifld_bbox(lat, lon)  # Queries a national HIFLD/Open layer with a 50 km envelope.
    nearest = None
    for feature in features:
        attrs = feature.get("attributes", {})
        voltage = read_voltage(attrs)
        if voltage is not None and voltage < 69:
            continue  # Keeps the grid-proximity signal focused on transmission-level substations when voltage is known.
        geom = feature.get("geometry", {})
        if "x" not in geom or "y" not in geom:
            continue
        dist = haversine_mi(lat, lon, geom["y"], geom["x"])
        if dist > 31.07:
            continue  # The bbox is only a fallback candidate search; the true radius remains 50 km.
        if nearest is None or dist < nearest["nearest_substation_distance_mi"]:
            nearest = {
                "nearest_substation_distance_mi": round(dist, 2),
                "voltage_kv": voltage if voltage is not None else "unavailable",
                "substation_name": attrs.get("NAME") or attrs.get("SUBSTATION") or "unavailable",
                "source": HIFLD_SUBSTATIONS_URL,
            }
    if nearest:
        return nearest
    return {"nearest_substation_distance_mi": "unavailable", "voltage_kv": "unavailable", "source": HIFLD_SUBSTATIONS_URL, "fallback": "No usable >=69kV substation feature found within 50 km."}


def fetch_hifld_bbox(lat: float, lon: float) -> list[dict[str, Any]]:
    lat_delta = 50 / 111.0
    lon_delta = 50 / (111.0 * max(math.cos(math.radians(lat)), 0.2))
    params = {
        "where": "1=1",
        "geometry": f"{lon - lon_delta},{lat - lat_delta},{lon + lon_delta},{lat + lat_delta}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "json",
    }  # Envelope query is a backup; haversine still enforces the true nearest distance.
    data, error = request_json(HIFLD_SUBSTATIONS_URL, params, "HIFLD substations bbox")
    if error:
        return []
    return data.get("features", []) if data else []


def fetch_usgs(lat: float, lon: float) -> dict[str, Any]:
    url = "https://epqs.nationalmap.gov/v1/json"
    data, error = request_json(url, {"x": lon, "y": lat, "units": "Meters", "wkid": 4326}, "USGS elevation")
    if error:
        return {"elevation_m": "unavailable", "source": url, "fallback": error}
    value = data.get("value") if data else None
    nested = (data or {}).get("USGS_Elevation_Point_Query_Service", {}).get("Elevation_Query", {})
    elevation = value if value is not None else nested.get("Elevation")
    try:
        return {"elevation_m": float(elevation), "source": url}
    except (TypeError, ValueError):
        return {"elevation_m": "unavailable", "source": url, "fallback": "USGS response did not include numeric elevation."}


def fetch_state(lat: float, lon: float) -> dict[str, Any]:
    url = "https://geo.fcc.gov/api/census/block/find"
    data, error = request_json(url, {"latitude": lat, "longitude": lon, "format": "json"}, "FCC state lookup")
    if error:
        return {"state": "unavailable", "fallback": error}
    state = (data or {}).get("State", {})
    return {"state": state.get("code") or "unavailable", "source": url}


def fetch_eia(lat: float, lon: float) -> dict[str, Any]:
    api_key = os.getenv("EIA_API_KEY")
    if not api_key:
        return {"regional_power_context": "unavailable", "fallback": "EIA_API_KEY missing."}
    state_info = fetch_state(lat, lon)
    state = state_info.get("state")
    if state == "unavailable":
        return {"regional_power_context": "unavailable", "fallback": state_info.get("fallback", "State lookup unavailable.")}
    url = "https://api.eia.gov/v2/electricity/retail-sales/data/"
    params = {
        "api_key": api_key,
        "frequency": "monthly",
        "data[0]": "price",
        "facets[stateid][]": state,
        "facets[sectorid][]": "ALL",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 1,
    }
    data, error = request_json(url, params, "EIA retail sales")
    if error:
        return {"regional_power_context": "unavailable", "state": state, "source": url, "fallback": error}
    rows = data.get("response", {}).get("data", []) if data else []
    if not rows:
        return {"regional_power_context": "unavailable", "state": state, "source": url, "fallback": "No EIA rows returned."}
    row = rows[0]
    return {"regional_power_context": {"state": state, "period": row.get("period"), "price": row.get("price"), "units": row.get("price-units")}, "source": url}


@tool
def get_site_data(lat: float, lon: float) -> dict[str, Any]:
    """Fetch real public siting signals for a coordinate, with unavailable fallbacks."""
    fema = fetch_fema(lat, lon)
    hifld = fetch_hifld(lat, lon)
    usgs = fetch_usgs(lat, lon)
    eia = fetch_eia(lat, lon)
    return {
        "lat": lat,
        "lon": lon,
        "flood": fema,
        "grid_proximity": hifld,
        "elevation": usgs,
        "regional_power": eia,
        "interconnection_capacity_mw": "unavailable",
        "interconnection_note": "Public APIs do not expose true available interconnection capacity; verify with the utility and ISO interconnection queue.",
    }


@tool
def search_zoning(query: str) -> str:
    """Search zoning.txt for cited local rules relevant to the user's site question."""
    docs = zoning_store.similarity_search(query, k=3)  # Retrieval can return the wrong chunk and the model may cite it confidently.
    labeled = [f"[chunk {i}] {doc.page_content}" for i, doc in enumerate(docs)]  # Chunk labels give the UI and final answer citation handles.
    return "\n\n".join(labeled)


def score_zoning(text: str) -> tuple[float, str]:
    lowered = text.lower()
    if any(word in lowered for word in ("prohibited", "not permitted", "forbidden")):
        return 0, "Zoning text appears prohibitive."
    if any(word in lowered for word in ("permitted", "allowed", "data center", "utility")):
        return 20, "Zoning text appears supportive or relevant."
    return 10, "Zoning fit is unknown from the supplied text."


def pick(site_data: dict[str, Any], flat_key: str, group: str, nested_key: str) -> Any:
    if flat_key in site_data:
        return site_data.get(flat_key)  # Handles model-supplied flattened score_site arguments.
    nested = site_data.get(group, {})
    return nested.get(nested_key) if isinstance(nested, dict) else None


@tool
def score_site(site_data: dict[str, Any]) -> dict[str, Any]:
    """Compute a deterministic 0-100 site score from public signals and zoning text."""
    zoning_text = str(site_data.get("zoning_text", ""))

    dist = pick(site_data, "nearest_substation_distance_mi", "grid_proximity", "nearest_substation_distance_mi")
    if isinstance(dist, (int, float)):
        grid_score = max(0, 35 * (1 - min(dist, 50) / 50))
    else:
        grid_score = 12.5  # Unavailable grid distance gets partial credit to avoid inventing a value.

    in_flood = pick(site_data, "in_fema_flood_zone", "flood", "in_fema_flood_zone")
    flood_score = 30 if in_flood is False else 0 if in_flood is True else 15

    elev = pick(site_data, "elevation_m", "elevation", "elevation_m")
    if isinstance(elev, (int, float)):
        elevation_score = 15 if elev >= 30 else 10 if elev >= 10 else 5
    else:
        elevation_score = 7.5

    zoning_score, zoning_method = score_zoning(zoning_text)
    total = round(grid_score + flood_score + elevation_score + zoning_score, 1)
    return {
        "score": total,
        "components": {
            "grid_proximity": round(grid_score, 1),
            "flood": flood_score,
            "elevation": elevation_score,
            "zoning": zoning_score,
        },
        "method": "Score = grid proximity up to 35, flood up to 30, elevation up to 15, zoning fit up to 20. " + zoning_method,
    }


@tool
def compare_sites(scored_sites: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank scored sites deterministically from highest score to lowest score."""
    ranked = sorted(scored_sites, key=lambda item: float(item.get("score", 0)), reverse=True)
    rows = []
    for index, site in enumerate(ranked, start=1):
        label = site.get("label") or site.get("site_key") or f"site {index}"
        rows.append({"rank": index, "label": label, "score": site.get("score"), "reason": f"Ranked by deterministic score {site.get('score')}."})
    return {"rankings": rows, "method": "Sorted scored_sites by score descending."}


def save_brief_data(brief: dict[str, Any]) -> dict[str, Any]:
    lat = brief.get("lat")
    lon = brief.get("lon")
    if lat is None or lon is None:
        return {"saved": False, "reason": "lat/lon missing"}
    site_key = key_for(float(lat), float(lon))
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO briefs(site_key, lat, lon, label, brief_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (site_key, lat, lon, brief.get("label"), json.dumps(brief, default=str), datetime.now(timezone.utc).isoformat()),
        )  # Upserts the latest brief for a rounded coordinate.
    return {"saved": True, "site_key": site_key}


@tool
def save_site_brief(brief: dict[str, Any]) -> dict[str, Any]:
    """Save a completed site brief in local SQLite memory."""
    return save_brief_data(brief)


@tool
def recall_site_briefs() -> dict[str, Any]:
    """Recall all saved site briefs from local SQLite memory."""
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT site_key, lat, lon, label, brief_json, created_at FROM briefs ORDER BY created_at DESC").fetchall()
    briefs = [{"site_key": row[0], "lat": row[1], "lon": row[2], "label": row[3], "brief": json.loads(row[4]), "created_at": row[5]} for row in rows]
    return {"briefs": briefs, "count": len(briefs)}


TOOLS = [get_site_data, search_zoning, score_site, compare_sites, save_site_brief, recall_site_briefs]
TOOL_BY_NAME = {item.name: item for item in TOOLS}
llm = ChatOpenAI(model=MODEL).bind_tools(TOOLS)  # Gives one model one shared set of tools for every mode.


def mode_prefix(mode_hint: str) -> str:
    hints = {
        "brief": "Create a concise grounded site brief.",
        "screen": "Screen the site or sites and produce a verdict.",
        "compare": "Compare the sites and rank them.",
        "diligence": "Run a deeper diligence memo with risks and human verification items.",
    }
    return hints.get(mode_hint, "")


def build_user_text(message: str, sites: list[dict[str, Any]], mode_hint: str) -> str:
    parts = []
    prefix = mode_prefix(mode_hint)
    if prefix:
        parts.append(prefix)
    if sites:
        parts.append("Confirmed map sites: " + json.dumps(sites))
    parts.append(message)
    return "\n".join(parts)


def history_messages(history: list[dict[str, str]]) -> list[Any]:
    built = []
    for item in history:
        if item.get("role") == "user":
            built.append(HumanMessage(item.get("content", "")))
        elif item.get("role") == "assistant":
            built.append(AIMessage(content=item.get("content", "")))
    return built


def record(trace: list[dict[str, Any]], loop: int, kind: str, tool_name: Optional[str], args: Any, result: Any, duration_ms: int) -> dict[str, Any]:
    event = {
        "step": len(trace) + 1,
        "loop": loop,
        "type": kind,
        "tool": tool_name,
        "args": args,
        "result_preview": preview(result),
        "duration_ms": duration_ms,
    }
    trace.append(event)
    return event


def emit(on_event, event: dict[str, Any]) -> None:
    if on_event:
        on_event(event)  # Streams trace rows as soon as they are created.


def final_result(messages: list[Any], score_value: Optional[float], trace: list[dict[str, Any]], loop: int, on_event=None) -> AgentResult:
    instruction = HumanMessage(
        "Produce the structured AgentResult. The reply must be at most 1-2 short plain sentences with no markdown, headings, lists, raw JSON, field names, or tool names. "
        "The reply must not restate the card details. Put details only in reasons and rankings. Each reason must be a human sentence grounded in a specific fact or zoning passage, using friendly source wording. "
        "Introduce no new facts."
    )
    structured = ChatOpenAI(model=MODEL).with_structured_output(AgentResult)
    start = time.perf_counter()
    result = structured.invoke(messages + [instruction])
    duration = int((time.perf_counter() - start) * 1000)
    if score_value is not None:
        result.score = score_value  # Pins the score from deterministic Python so model output cannot drift.
    emit(on_event, record(trace, loop, "final", None, {}, result.model_dump(), duration))
    return result


def save_completed(result: AgentResult, sites: list[dict[str, Any]], scored_sites: list[dict[str, Any]]) -> None:
    if len(sites) == 1 and result.score is not None:
        site = sites[0]
        brief = result.model_dump()
        brief.update({"lat": site["lat"], "lon": site["lon"], "label": site.get("label")})  # Adds the memory key fields to the structured brief.
        save_brief_data(brief)
        return
    for item in scored_sites:
        if item.get("lat") is not None and item.get("lon") is not None:
            save_brief_data(item)  # Multi-site runs save each deterministic scored site for later recall.


def history_count() -> int:
    return recall_site_briefs.invoke({})["count"]  # Reuses the memory tool implementation for the API header.


def run_agent(message: str, sites: Optional[list[dict[str, Any]]] = None, mode_hint: str = "auto", history: Optional[list[dict[str, str]]] = None, on_event=None) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []
    score_value = None
    scored_sites: list[dict[str, Any]] = []
    sites = sites or []
    history = history or []
    messages = [SystemMessage(SYSTEM_PROMPT), *history_messages(history), HumanMessage(build_user_text(message, sites, mode_hint))]

    # Explicit agent loop: one model, one tool set, repeated until the model stops calling tools.
    for loop in range(1, 13):
        start = time.perf_counter()
        msg = llm.invoke(messages)
        duration = int((time.perf_counter() - start) * 1000)
        messages.append(msg)
        emit(on_event, record(trace, loop, "model_call", None, {}, msg.content or msg.tool_calls, duration))

        if not msg.tool_calls:
            reply = msg.content or ""
            needs_input = score_value is None and not sites
            score_pin = score_value if len(sites) == 1 else None
            result_obj = None if needs_input else final_result(messages, score_pin, trace, loop, on_event)
            if result_obj is not None and len(sites) != 1:
                result_obj.score = None  # Compare outputs do not get a model-written top-level score.
            if result_obj is not None:
                save_completed(result_obj, sites, scored_sites)
            return {
                "reply": reply if needs_input else result_obj.reply,
                "result": None if result_obj is None else result_obj.model_dump(),
                "needs_input": needs_input,
                "trace": trace,
                "loops": loop,
                "history_count": history_count(),
            }

        for call in msg.tool_calls:
            tool_obj = TOOL_BY_NAME[call["name"]]
            start = time.perf_counter()
            result = tool_obj.invoke(call["args"])
            duration = int((time.perf_counter() - start) * 1000)
            if call["name"] == "score_site":
                score_value = result.get("score")
                site_arg = call["args"].get("site_data", {})
                fallback_site = sites[len(scored_sites)] if len(scored_sites) < len(sites) else {}
                scored_sites.append(
                    {
                        "lat": site_arg.get("lat", fallback_site.get("lat")),
                        "lon": site_arg.get("lon", fallback_site.get("lon")),
                        "label": fallback_site.get("label"),
                        "kind": "scored_site",
                        "score": result.get("score"),
                        "score_result": result,
                    }
                )  # Captures each deterministic score so memory is not left to model behavior.
            emit(on_event, record(trace, loop, "tool_call", call["name"], call["args"], result, duration))
            messages.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=call["id"]))

    return {
        "reply": "The agent reached its loop limit before completing. Please narrow the request.",
        "result": None,
        "needs_input": True,
        "trace": trace,
        "loops": 12,
        "history_count": history_count(),
    }
