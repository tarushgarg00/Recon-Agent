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
DB_PATH = os.path.join("/tmp", "sites.db") if os.getenv("VERCEL") else "sites.db"  # Vercel functions can only write SQLite files under /tmp.
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


class ScoreComponents(BaseModel):
    grid_proximity: Optional[float] = Field(default=None, description="Deterministic grid proximity component when present.")
    flood: Optional[float] = Field(default=None, description="Deterministic flood component when present.")
    elevation: Optional[float] = Field(default=None, description="Deterministic elevation component when present.")
    zoning: Optional[float] = Field(default=None, description="Deterministic zoning component when present.")


class CheckItem(BaseModel):
    label: str = Field(description="Short pass/fail check label.")
    status: str = Field(description="pass, caution, fail, or unavailable.")
    detail: str = Field(description="One-line explanation of the check.")
    grounded_in: str = Field(description="Source used for the check.")


class RiskItem(BaseModel):
    severity: str = Field(description="low, moderate, or high.")
    risk: str = Field(description="Risk name.")
    why: str = Field(description="Why this is risky for development.")
    grounded_in: str = Field(description="Source used for the risk.")


class RankedSite(BaseModel):
    label: str = Field(description="Site label or rounded coordinate.")
    score: float = Field(description="Deterministic score from score_site.")
    reason: str = Field(description="Short deterministic ranking reason.")
    lat: Optional[float] = Field(default=None, description="Site latitude when available.")
    lon: Optional[float] = Field(default=None, description="Site longitude when available.")
    findings: list[Reason] = Field(default_factory=list, description="Per-site factual findings for comparison expansion.")
    score_components: ScoreComponents = Field(default_factory=ScoreComponents, description="Deterministic score_site components.")


class AgentResult(BaseModel):
    kind: str = Field(description="brief, screen, compare, diligence, question, or memo.")
    reply: str = Field(description="Natural-language answer for the user.")
    verdict: Optional[str] = Field(default=None, description="GO, CAUTION, or NO-GO when applicable.")
    score: Optional[float] = Field(default=None, description="Deterministic score when one site is evaluated.")
    reasons: list[Reason] = Field(default_factory=list, description="Grounded reasons for the result.")
    rankings: list[RankedSite] = Field(default_factory=list, description="Ranked sites for comparisons.")
    checks: list[CheckItem] = Field(default_factory=list, description="Short pass/fail checks for quick screen mode.")
    risks: list[RiskItem] = Field(default_factory=list, description="Risk register rows for risk deep dive mode.")
    next_steps: list[str] = Field(default_factory=list, description="Recommended next steps for full diligence mode.")
    history_note: Optional[str] = Field(default=None, description="How this site compares to saved scored sites.")
    score_components: ScoreComponents = Field(default_factory=ScoreComponents, description="Deterministic top-level score components.")
    needs_human_review: str = Field(description="What a human must verify before relying on the result.")


SYSTEM_PROMPT = """
You are a data center siting analyst for auditable site screening.
Use real tools for facts and never invent data values.
Ask a clarifying question if you lack a coordinate or confirmed site.
Use get_site_data for public facts, search_zoning for legal rules, score_site for scores, compare_sites for rankings, and memory tools for saved sites.
Use plain English in user-facing text. Do not show raw JSON, field names, tool names, markdown headings, bullets, or numbered lists.
True interconnection capacity is not available from public APIs; always say a human must verify capacity with the utility or ISO queue.
Use score_site for numeric scores and compare_sites for rankings; never compute scores or ranks yourself.
Comparisons require 2 or more distinct confirmed sites, and each distinct site must be scored with score_site before compare_sites is called.
When a coordinate already exists in memory, recall it instead of recomputing.
When recalling saved sites, mention the stored score and verdict if the memory tool returns them.
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
        rows.append(
            {
                "rank": index,
                "label": label,
                "score": site.get("score"),
                "reason": site.get("reason") or f"Ranked by deterministic score {site.get('score')}.",
                "lat": site.get("lat"),
                "lon": site.get("lon"),
                "findings": site.get("findings", []),
                "score_components": site.get("score_result", {}).get("components", {}),
            }
        )
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
        "brief": "Run a full diligence evaluation. Gather flood, grid, elevation, power context, zoning, score, and next-step evidence.",
        "screen": "Run a fast go/no-go screen. Keep the final artifact short with only threshold-style checks and a clear verdict.",
        "compare": "Compare the confirmed sites. Require 2 or more distinct sites and score each distinct site before ranking them.",
        "diligence": "Run a risk deep dive. Lead with risks, gaps, severity, and what must be verified; de-emphasize positives.",
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
        "result": result,  # Gives the UI real source data for report coverage without changing tool behavior.
        "duration_ms": duration_ms,
        "phase": "complete",
    }
    trace.append(event)
    return event


def emit(on_event, event: dict[str, Any]) -> None:
    if on_event:
        on_event(event)  # Streams trace rows as soon as they are created.


def final_result(messages: list[Any], score_value: Optional[float], trace: list[dict[str, Any]], loop: int, mode_hint: str, on_event=None) -> AgentResult:
    mode_guidance = {
        "screen": "For screen mode, keep the final output short: verdict, score, and brief pass/fail checks only.",
        "brief": "For full diligence mode, include comprehensive findings, score breakdown context, and recommended next steps.",
        "diligence": "For risk deep dive mode, write like a risk register with severity, gaps, and verification needs.",
        "compare": "For comparison mode, use rankings only from compare_sites.",
    }.get(mode_hint, "Produce a concise Recon analysis.")
    instruction = HumanMessage(
        "Produce the structured AgentResult. The reply must be at most 1-2 short plain sentences with no markdown, headings, lists, raw JSON, field names, or tool names. "
        "The reply must not restate the card details. Put details only in reasons and rankings. Each reason must be a human sentence grounded in a specific fact or zoning passage, using friendly source wording. "
        f"{mode_guidance} "
        "For comparisons, use the compare_sites ranking order and scores exactly; do not say sites are tied unless the deterministic scores are equal. "
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


def distinct_sites(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    built = []
    for site in sites:
        lat = site.get("lat")
        lon = site.get("lon")
        if lat is None or lon is None:
            continue
        key = key_for(float(lat), float(lon))
        if key in seen:
            continue
        seen.add(key)
        built.append({"lat": float(lat), "lon": float(lon), "label": site.get("label") or key})
    return built


def score_text(score: Any) -> str:
    try:
        return f"{float(score):.1f}"
    except (TypeError, ValueError):
        return "unavailable"


def source_findings(site_data: dict[str, Any], zoning_text: str, score_result: dict[str, Any]) -> list[dict[str, str]]:
    flood = site_data.get("flood", {})
    grid = site_data.get("grid_proximity", {})
    elevation = site_data.get("elevation", {})
    in_flood = flood.get("in_fema_flood_zone")
    flood_text = "Flood data is unavailable for this site."
    if in_flood is True:
        flood_text = f"FEMA maps show flood zone {flood.get('flood_zone', 'unavailable')} at this site."
    elif in_flood is False:
        flood_text = "FEMA maps do not show the site in a mapped flood zone."

    dist = grid.get("nearest_substation_distance_mi")
    voltage = grid.get("voltage_kv")
    if isinstance(dist, (int, float)) and "fallback" not in grid:
        grid_text = f"Nearest HIFLD substation is {dist} miles away"
        if voltage != "unavailable":
            grid_text += f" at {voltage} kV"
        grid_text += "."
    else:
        grid_text = "HIFLD did not return a usable transmission-level substation within 50 km."

    elev = elevation.get("elevation_m")
    elevation_text = f"USGS elevation is {round(elev, 1)} meters." if isinstance(elev, (int, float)) and "fallback" not in elevation else "USGS elevation is unavailable for this site."
    zoning_text_clean = str(zoning_text).strip()
    zoning_method = score_result.get("method", "").split(". ")[-1] if zoning_text_clean else "Zoning ordinance was not checked for this site."

    return [
        {"point": flood_text, "grounded_in": "FEMA flood maps"},
        {"point": grid_text, "grounded_in": "HIFLD grid data"},
        {"point": elevation_text, "grounded_in": "USGS elevation"},
        {"point": zoning_method, "grounded_in": f"Zoning ordinance: {preview(zoning_text, 180)}" if zoning_text_clean else "Zoning ordinance"},
    ]


def ranking_reason(row: dict[str, Any], index: int, top_tie_score: Optional[Any], previous: Optional[dict[str, Any]] = None) -> str:
    score = score_text(row.get("score"))
    if top_tie_score is not None and row.get("score") == top_tie_score:
        return f"Tied at deterministic score {score}."
    if index == 0:
        return f"Ranked first by deterministic score {score}."
    if previous and row.get("score") == previous.get("score"):
        return f"Tied with {previous.get('label')} at deterministic score {score}."
    return f"Ranked by deterministic score {score}."


def compare_reply(rankings: list[dict[str, Any]]) -> str:
    if len(rankings) < 2:
        return "Recon needs at least two confirmed sites to compare."
    first, second = rankings[0], rankings[1]
    if first.get("score") == second.get("score"):
        return f"{first.get('label')} and {second.get('label')} are tied at {score_text(first.get('score'))}."
    return f"{first.get('label')} ranks first over {second.get('label')} by deterministic score {score_text(first.get('score'))} vs {score_text(second.get('score'))}."


def site_key_from_data(site_data: dict[str, Any]) -> Optional[str]:
    lat = site_data.get("lat")
    lon = site_data.get("lon")
    if lat is None or lon is None:
        return None
    return key_for(float(lat), float(lon))


def fallback_site(scored_sites: list[dict[str, Any]], sites: list[dict[str, Any]]) -> dict[str, Any]:
    scored_keys = {key_for(float(item["lat"]), float(item["lon"])) for item in scored_sites if item.get("lat") is not None and item.get("lon") is not None}
    for site in sites:
        if key_for(site["lat"], site["lon"]) not in scored_keys:
            return site
    return {}


def scored_site_from_tool(call_args: dict[str, Any], result: dict[str, Any], sites: list[dict[str, Any]], scored_sites: list[dict[str, Any]]) -> dict[str, Any]:
    site_data = call_args.get("site_data", {})
    fallback = fallback_site(scored_sites, sites)
    lat = site_data.get("lat", fallback.get("lat"))
    lon = site_data.get("lon", fallback.get("lon"))
    matched = next((site for site in sites if lat is not None and lon is not None and key_for(site["lat"], site["lon"]) == key_for(float(lat), float(lon))), fallback)
    label = matched.get("label") or (key_for(float(lat), float(lon)) if lat is not None and lon is not None else "unlabeled site")
    return {
        "lat": lat,
        "lon": lon,
        "label": label,
        "kind": "scored_site",
        "score": result.get("score"),
        "site_data": site_data,
        "zoning_text": site_data.get("zoning_text", ""),
        "score_result": result,
        "findings": source_findings(site_data, site_data.get("zoning_text", ""), result),
    }


def validated_compare_args(sites: list[dict[str, Any]], scored_sites: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    required = {key_for(site["lat"], site["lon"]): site for site in sites}
    scored = {key_for(float(site["lat"]), float(site["lon"])): site for site in scored_sites if site.get("lat") is not None and site.get("lon") is not None}
    missing = [site.get("label") or key for key, site in required.items() if key not in scored]
    incomplete = []
    for key, site in scored.items():
        site_data = site.get("site_data", {})
        has_public_data = all(isinstance(site_data.get(name), dict) for name in ("flood", "grid_proximity", "elevation"))
        if key in required and not has_public_data:
            incomplete.append(site.get("label") or key)
    if len(required) > 1 and (missing or incomplete):
        return None, {
            "error": "Comparison is not ready.",
            "missing_sites": missing,
            "incomplete_sites": incomplete,
            "next_step": "For each listed confirmed site, call get_site_data, then score_site with those public facts before calling compare_sites again. Add zoning text when available.",
        }  # Validation rejects thin lat/lon-only scores while leaving the model responsible for the next tool calls.
    return {"scored_sites": list(scored.values()) if required else scored_sites}, None


def score_args_error(call_args: dict[str, Any], sites: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not sites:
        return None
    site_data = call_args.get("site_data", {})
    has_public_data = all(isinstance(site_data.get(name), dict) for name in ("flood", "grid_proximity", "elevation"))
    if has_public_data:
        return None
    return {
        "error": "Score is not ready.",
        "next_step": "Call get_site_data for the confirmed site, then call score_site with those public facts.",
    }  # Validation keeps thin lat/lon-only score calls from becoming final deterministic scores.


def scored_for_single_site(sites: list[dict[str, Any]], scored_sites: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if len(sites) != 1:
        return None
    site_key = key_for(sites[0]["lat"], sites[0]["lon"])
    return next((item for item in reversed(scored_sites) if site_key_from_data(item) == site_key), None)


def components_from_score(score_result: dict[str, Any]) -> ScoreComponents:
    return ScoreComponents.model_validate(score_result.get("components", {}))


def history_note_for(site: dict[str, Any], score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    memory = recall_site_briefs.invoke({})
    scored = []
    current_key = key_for(site["lat"], site["lon"])
    for item in memory.get("briefs", []):
        brief = item.get("brief", {})
        saved_score = brief.get("score")
        if saved_score is None or item.get("site_key") == current_key:
            continue
        try:
            scored.append({"label": item.get("label") or item.get("site_key"), "score": float(saved_score)})
        except (TypeError, ValueError):
            continue
    if not scored:
        return "This is the first scored site in your saved history."
    all_scores = scored + [{"label": site.get("label") or current_key, "score": float(score)}]
    ranked = sorted(all_scores, key=lambda item: item["score"], reverse=True)
    rank = next(index for index, item in enumerate(ranked, start=1) if item["label"] == (site.get("label") or current_key) and item["score"] == float(score))
    best = ranked[0]
    if rank == 1:
        return f"Ranks 1st of {len(ranked)} saved scored sites."
    return f"Ranks {rank} of {len(ranked)} saved scored sites, below your best: {best['label']} at {score_text(best['score'])}."


def screen_checks(scored: dict[str, Any]) -> list[CheckItem]:
    site_data = scored.get("site_data", {})
    flood = site_data.get("flood", {})
    grid = site_data.get("grid_proximity", {})
    elevation = site_data.get("elevation", {})
    in_flood = flood.get("in_fema_flood_zone")
    dist = grid.get("nearest_substation_distance_mi")
    elev = elevation.get("elevation_m")
    flood_status = "pass" if in_flood is False else "fail" if in_flood is True else "unavailable"
    grid_status = "pass" if isinstance(dist, (int, float)) and dist <= 5 else "caution" if isinstance(dist, (int, float)) and dist <= 15 else "unavailable"
    elevation_status = "pass" if isinstance(elev, (int, float)) and elev >= 30 else "caution" if isinstance(elev, (int, float)) and elev >= 10 else "fail" if isinstance(elev, (int, float)) else "unavailable"
    return [
        CheckItem(label="Flood", status=flood_status, detail="FEMA maps do not show mapped flood overlap." if flood_status == "pass" else "FEMA flood status needs review.", grounded_in="FEMA flood maps"),
        CheckItem(label="Grid", status=grid_status, detail=f"Nearest HIFLD substation is {dist} miles away." if isinstance(dist, (int, float)) else "HIFLD grid distance is unavailable.", grounded_in="HIFLD grid data"),
        CheckItem(label="Elevation", status=elevation_status, detail=f"USGS elevation is {round(elev, 1)} meters." if isinstance(elev, (int, float)) else "USGS elevation is unavailable.", grounded_in="USGS elevation"),
    ]


def risk_items(scored: dict[str, Any]) -> list[RiskItem]:
    site_data = scored.get("site_data", {})
    flood = site_data.get("flood", {})
    grid = site_data.get("grid_proximity", {})
    elevation = site_data.get("elevation", {})
    power = site_data.get("regional_power", {})
    dist = grid.get("nearest_substation_distance_mi")
    in_flood = flood.get("in_fema_flood_zone")
    elev = elevation.get("elevation_m")
    flood_severity = "high" if in_flood is True else "moderate" if in_flood == "unavailable" else "low"
    grid_available = isinstance(dist, (int, float)) and "fallback" not in grid
    elevation_available = isinstance(elev, (int, float)) and "fallback" not in elevation
    grid_severity = "low" if grid_available and dist <= 5 else "moderate" if grid_available else "high"
    elevation_severity = "moderate" if elevation_available and elev < 10 else "low" if elevation_available else "moderate"
    return [
        RiskItem(severity=flood_severity, risk="Flood exposure", why="Mapped flood status can affect site design, insurance, and permitting.", grounded_in="FEMA flood maps"),
        RiskItem(severity=grid_severity, risk="Interconnection uncertainty", why="Public substation proximity is not the same as available capacity; utility and ISO queue review is required.", grounded_in="HIFLD grid data"),
        RiskItem(severity=elevation_severity, risk="Elevation and drainage", why="Low or unavailable elevation increases civil and flood-engineering uncertainty.", grounded_in="USGS elevation"),
        RiskItem(severity="moderate", risk="Power market context", why=power.get("fallback", "Regional public power context is a screening signal, not a deliverability study."), grounded_in="EIA regional data"),
        RiskItem(severity="moderate", risk="Zoning and permitting", why="The local ordinance still needs expert review before relying on use permissibility.", grounded_in="Zoning ordinance"),
    ]


def next_steps(scored: dict[str, Any]) -> list[str]:
    return [
        "Ask the utility or ISO to verify available interconnection capacity and queue position.",
        "Have local counsel confirm zoning permissibility and any special-use or site-plan requirements.",
        "Commission flood, civil, and geotechnical diligence before relying on public screening data.",
    ]


def risk_verdict(risks: list[RiskItem], score: Optional[float]) -> str:
    severities = [item.severity.lower() for item in risks]
    high_risks = [item for item in risks if item.severity.lower() == "high"]
    high_flood = any("flood" in item.risk.lower() for item in high_risks)
    try:
        numeric_score = float(score) if score is not None else None
    except (TypeError, ValueError):
        numeric_score = None
    if high_flood and (numeric_score is None or numeric_score < 60):
        return "NO-GO"
    if "high" in severities:
        return "CAUTION"
    if numeric_score is not None and numeric_score >= 85:
        return "GO"
    return "CAUTION"


def risk_reply(verdict: str, risks: list[RiskItem]) -> str:
    high = [item.risk for item in risks if item.severity.lower() == "high"]
    if verdict == "GO":
        return "The risk register shows no high-severity risks; continue with normal verification of interconnection capacity, zoning, and civil diligence."
    if verdict == "NO-GO":
        subject = high[0] if high else "a high-severity risk"
        return f"The risk register flags {subject} as high severity; do not advance without resolving that issue."
    if high:
        return f"The risk register includes high-severity concerns around {', '.join(high)} that require focused review."
    return "The risk register shows moderate concerns that should be reviewed before relying on the site."


def enrich_single_site_result(result: AgentResult, mode_hint: str, sites: list[dict[str, Any]], scored_sites: list[dict[str, Any]]) -> None:
    scored = scored_for_single_site(sites, scored_sites)
    if not scored:
        return
    result.kind = {"screen": "screen", "brief": "brief", "diligence": "diligence"}.get(mode_hint, result.kind)
    result.score_components = components_from_score(scored.get("score_result", {}))
    result.history_note = history_note_for(sites[0], result.score)
    if mode_hint == "screen":
        result.checks = screen_checks(scored)
        result.risks = []
        result.next_steps = []
        result.reasons = []
    elif mode_hint == "diligence":
        result.risks = risk_items(scored)
        result.verdict = risk_verdict(result.risks, result.score)  # Pins risk-mode verdict from register severities so the card cannot contradict the risks below.
        result.reply = risk_reply(result.verdict, result.risks)
        result.checks = []
        result.next_steps = next_steps(scored)
        result.reasons = []
    elif mode_hint == "brief":
        result.reasons = [Reason.model_validate(item) for item in scored.get("findings", [])]
        result.checks = []
        result.risks = []
        result.next_steps = next_steps(scored)


def run_agent(message: str, sites: Optional[list[dict[str, Any]]] = None, mode_hint: str = "auto", history: Optional[list[dict[str, str]]] = None, on_event=None) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []
    score_value = None
    scored_sites: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    sites = distinct_sites(sites or [])
    history = history or []
    wants_compare = mode_hint == "compare" or any(word in message.lower() for word in ("compare", "rank", "ranking"))

    messages = [SystemMessage(SYSTEM_PROMPT), *history_messages(history), HumanMessage(build_user_text(message, sites, mode_hint))]

    # Explicit agent loop: one model, one tool set, repeated until the model stops calling tools.
    for loop in range(1, 13):
        start = time.perf_counter()
        msg = llm.invoke(messages)
        duration = int((time.perf_counter() - start) * 1000)
        messages.append(msg)
        emit(on_event, record(trace, loop, "model_call", None, {}, msg.content or msg.tool_calls, duration))

        if not msg.tool_calls:
            if mode_hint == "compare" and len(sites) < 2:
                reply = msg.content or "Add 2+ confirmed sites to compare."
                return {
                    "reply": reply,
                    "result": None,
                    "needs_input": True,
                    "trace": trace,
                    "loops": loop,
                    "event_count": len(trace),
                    "history_count": history_count(),
                }
            if wants_compare and len(sites) >= 2 and len(comparison_rows) < 2:
                messages.append(
                    HumanMessage(
                        "Continue the comparison with tools. Score each distinct confirmed site with score_site, then call compare_sites with those scored sites before finalizing."
                    )
                )  # Validation keeps the agent in the loop instead of accepting an incomplete comparison.
                continue
            if len(sites) == 1 and score_value is None and any(event.get("tool") == "get_site_data" for event in trace):
                messages.append(
                    HumanMessage("Continue with tools. Call score_site using the public facts returned by get_site_data before finalizing this single-site evaluation.")
                )  # Prevents a no-score artifact after the model made a thin score_site call and received a tool error.
                continue
            reply = msg.content or ""
            needs_input = score_value is None and not sites
            score_pin = score_value if len(sites) == 1 else None
            result_obj = None if needs_input else final_result(messages, score_pin, trace, loop, mode_hint, on_event)
            if result_obj is not None and comparison_rows:
                result_obj.kind = "compare"
                result_obj.rankings = [RankedSite.model_validate(row) for row in comparison_rows]
                result_obj.score = None  # Compare artifacts use per-site deterministic scores, not a top-level model score.
            elif result_obj is not None and len(sites) <= 1:
                result_obj.rankings = []  # A single-site artifact must not leak a one-row ranking UI.
                enrich_single_site_result(result_obj, mode_hint, sites, scored_sites)
            elif result_obj is not None and len(sites) != 1:
                result_obj.score = None  # Compare outputs do not get a model-written top-level score.
            if result_obj is not None:
                save_completed(result_obj, sites, scored_sites)
            return {
                "reply": reply if needs_input else result_obj.reply,
                "result": None if result_obj is None else result_obj.model_dump(),
                "needs_input": needs_input,
                "trace": trace,
                "loops": loop,
                "event_count": len(trace),
                "history_count": history_count(),
            }

        for call in msg.tool_calls:
            tool_obj = TOOL_BY_NAME[call["name"]]
            start = time.perf_counter()
            if call["name"] == "compare_sites":
                compare_args, error = validated_compare_args(sites, scored_sites)
                if error:
                    result = error  # Bad compare inputs are returned as tool data so the model can recover in the next loop.
                else:
                    result = tool_obj.invoke(compare_args)
                    comparison_rows = result.get("rankings", [])
                    top_tie_score = comparison_rows[0].get("score") if len(comparison_rows) > 1 and comparison_rows[0].get("score") == comparison_rows[1].get("score") else None
                    previous = None
                    for index, row in enumerate(comparison_rows):
                        row["reason"] = ranking_reason(row, index, top_tie_score if index < 2 else None, previous)
                        previous = row
            elif call["name"] == "score_site":
                error = score_args_error(call["args"], sites)
                result = error if error else tool_obj.invoke(call["args"])
            else:
                result = tool_obj.invoke(call["args"])
            duration = int((time.perf_counter() - start) * 1000)
            if call["name"] == "score_site" and "score" in result:
                score_value = result.get("score")
                scored = scored_site_from_tool(call["args"], result, sites, scored_sites)
                scored_key = site_key_from_data(scored)
                existing = next((index for index, item in enumerate(scored_sites) if scored_key and site_key_from_data(item) == scored_key), None)
                if existing is None:
                    scored_sites.append(scored)
                else:
                    scored_sites[existing] = scored
                # Captures each deterministic score with its coordinate so compare_sites cannot rank duplicated or missing sites.
            emit(on_event, record(trace, loop, "tool_call", call["name"], call["args"], result, duration))
            messages.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=call["id"]))

    return {
        "reply": "The agent reached its loop limit before completing. Please narrow the request.",
        "result": None,
        "needs_input": True,
        "trace": trace,
        "loops": 12,
        "event_count": len(trace),
        "history_count": history_count(),
    }
