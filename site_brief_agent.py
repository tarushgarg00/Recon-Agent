import json
import os
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field


MODEL = "gpt-4o-mini"


load_dotenv()  # Loads OPENAI_API_KEY from .env so LangChain can call OpenAI.


ZONING_TEXT = open("zoning.txt", encoding="utf-8").read()  # Loads the real ordinance text from your local project file.


splitter = RecursiveCharacterTextSplitter(
    chunk_size=800,
    chunk_overlap=120,
)  # Splits long ordinance text into overlapping chunks for retrieval.
chunks = splitter.create_documents([ZONING_TEXT])  # Wraps each chunk as a Document for the vector store.
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")  # Embeds chunks so similar legal text can be found.
zoning_store = InMemoryVectorStore.from_documents(chunks, embeddings)  # Stores vectors in memory for this run only.


class Reason(BaseModel):
    point: str = Field(description="A short reason supporting the site verdict.")
    grounded_in: str = Field(description="The exact fact or zoning chunk this reason rests on.")


class SiteBrief(BaseModel):
    verdict: str = Field(description="One of: GO, CAUTION, or NO-GO.")
    score: float = Field(description="The deterministic score returned by score_power_fit.")
    reasons: list[Reason] = Field(description="Reasons tied to specific facts or retrieved zoning chunks.")
    needs_human_review: str = Field(description="What an expert must verify before trusting this brief.")


def fetch_available_power_mw() -> float:
    api_key = os.getenv("EIA_API_KEY")  # EIA open data requires a free API key, so this stays optional.
    if not api_key:
        return 120  # Falls back to the teaching mock when no EIA key is configured.

    params = urlencode(
        {
            "api_key": api_key,
            "frequency": "monthly",
            "data[0]": "capacity",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "offset": 0,
            "length": 1,
        }
    )  # Requests the latest capacity row from EIA's open electricity capability route.
    url = f"https://api.eia.gov/v2/electricity/operating-generator-capacity/data/?{params}"

    try:
        with urlopen(url, timeout=5) as response:  # Calls the public HTTP source with a short timeout.
            payload = json.loads(response.read().decode("utf-8"))  # Parses the JSON response into Python data.
        return float(payload["response"]["data"][0]["capacity"])  # Uses EIA capacity as available MW for this mock.
    except Exception:
        return 120  # Falls back to the teaching mock if the network or EIA response fails.


@tool
def get_site_data(lat: float, lon: float) -> dict:
    """Get mock site facts for a latitude and longitude."""
    # This mock stands in for real sources like EIA, HIFLD, and FEMA.
    return {
        "available_power_mw": fetch_available_power_mw(),
        "distance_to_substation_mi": 2.4,
        "in_fema_flood_zone": False,
        "grid_operator": "RTE France",
    }


@tool
def search_zoning(query: str) -> str:
    """Search the pasted zoning text for legal rules relevant to a site question."""
    docs = zoning_store.similarity_search(query, k=3)  # Retrieve can return the WRONG chunk and the model may cite it confidently (failure mode #2).
    labeled = [f"[chunk {i}] {doc.page_content}" for i, doc in enumerate(docs)]  # Labels chunks so the model can cite them.
    return "\n\n".join(labeled)  # Returns plain text because the final brief needs quotable rule snippets.


@tool
def score_power_fit(available_power_mw: float, distance_to_substation_mi: float) -> dict:
    """Score power availability and grid proximity with deterministic Python math."""
    power_points = min(available_power_mw / 100, 1) * 70  # Gives full power credit at 100 MW or more.
    proximity_ratio = max(0, 1 - (distance_to_substation_mi / 10))  # Gives closer sites more credit, fading to zero at 10 miles.
    proximity_points = proximity_ratio * 30  # Caps proximity at 30 percent of the score.
    score = round(power_points + proximity_points)  # Produces a stable 0-100 integer score.
    return {
        "power_fit_score": score,
        "method": "Power is capped at 70 points: min(available_power_mw / 100, 1) * 70. Proximity is capped at 30 points: max(0, 1 - distance_to_substation_mi / 10) * 30.",
    }


tools = {
    "get_site_data": get_site_data,
    "search_zoning": search_zoning,
    "score_power_fit": score_power_fit,
}
llm = ChatOpenAI(model=MODEL).bind_tools([get_site_data, search_zoning, score_power_fit])  # bind_tools tells the model which local tools it may request.


def gather(user_msg: str):
    gather.power_fit_score = None  # Resets the captured deterministic score for this run.
    messages = [
        SystemMessage(
            "data center siting analyst, use tools for facts, never invent numbers; "
            "use search_zoning for legal rules and quote what it returns; "
            "use score_power_fit for the score; never compute a score yourself"
        ),
        HumanMessage(user_msg),
    ]

    # Explicit agent loop: ask model, run requested tools, append tool results, repeat.
    while True:
        msg = llm.invoke(messages)  # Gets the model's next step from the current transcript.
        messages.append(msg)  # Keeps the AIMessage so later calls see the tool request.

        if not msg.tool_calls:
            return messages  # Ends when the model stops asking for tools.

        for call in msg.tool_calls:
            chosen_tool = tools[call["name"]]  # Finds the local tool the model requested by name.
            result = chosen_tool.invoke(call["args"])  # Runs the tool with model-supplied arguments.
            if call["name"] == "score_power_fit":
                gather.power_fit_score = result["power_fit_score"]  # Captures the deterministic score from the tool.
            messages.append(
                ToolMessage(
                    content=str(result),
                    tool_call_id=call["id"],
                )
            )


def brief(user_msg: str) -> SiteBrief:
    messages = gather(user_msg)  # First lets the explicit tool loop collect facts, rules, and score.
    messages.append(
        HumanMessage(
            "Produce the final SiteBrief. Ground every reason in a specific fact or retrieved passage. "
            "Introduce no new facts."
        )
    )  # Adds a final formatting instruction after tool use is complete.
    structured_llm = ChatOpenAI(model=MODEL).with_structured_output(SiteBrief)  # Forces the final answer into the Pydantic schema.
    brief_obj = structured_llm.invoke(messages)  # Returns a typed SiteBrief object instead of free-form prose.
    if gather.power_fit_score is None:
        raise ValueError("score_power_fit did not run")
    brief_obj.score = gather.power_fit_score  # Pins score from the deterministic tool so the model cannot drift.
    return brief_obj


def evals() -> None:
    cases = [
        (
            "GO case",
            "Evaluate 48.06, -1.70 for a 100MW data center. The buy box allows substations within 5 miles. Check power, check zoning, and score it.",
            "GO",
        ),
        (
            "NO-GO case",
            "Evaluate 48.06, -1.70 for a 100MW data center. The buy box rejects any site more than 1 mile from a substation. Check power, check zoning, and score it.",
            "NO-GO",
        ),
    ]

    for name, prompt, expected in cases:
        try:
            out = brief(prompt)  # Runs the full agent flow for this teaching eval case.
            assert out.verdict == expected  # Checks the structured verdict against the expected label.
            print(f"{name}: pass")  # Prints a compact success line for the case.
        except Exception as exc:
            print(f"{name}: fail ({exc})")  # Prints failures without stopping the remaining cases.


if __name__ == "__main__":
    out = brief(
        "Evaluate 48.06, -1.70 for a 100MW data center. Check power, check zoning, and score it."
    )  # Runs one end-to-end teaching example.
    print(out.model_dump_json(indent=2))  # Prints readable JSON from the structured Pydantic result.
