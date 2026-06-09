import json

from dotenv import load_dotenv
from openai import OpenAI


MODEL = "gpt-4o-mini"


load_dotenv()  # Loads OPENAI_API_KEY from .env so this script stays local-only.
client = OpenAI()  # Creates the raw OpenAI SDK client using environment credentials.


def get_site_data(lat, lon):
    # This mock stands in for real sources like EIA, HIFLD, and FEMA.
    return {
        "available_power_mw": 120,
        "distance_to_substation_mi": 2.4,
        "in_fema_flood_zone": False,
        "grid_operator": "RTE France",
    }


tools = [
    {
        "type": "function",
        "function": {
            "name": "get_site_data",
            "description": "Get basic site facts for a latitude and longitude.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude of the candidate site.",
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude of the candidate site.",
                    },
                },
                "required": ["lat", "lon"],
                "additionalProperties": False,
            },
        },
    }
]


def run(user_msg):
    messages = [
        {
            "role": "system",
            "content": "data center siting analyst, use tools for facts, never invent numbers",
        },
        {"role": "user", "content": user_msg},
    ]

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
        )
        msg = response.choices[0].message  # Pulls out the model's next step.
        messages.append(msg.model_dump(exclude_none=True))  # Keeps full assistant state.

        if not msg.tool_calls:
            return msg.content or ""  # Ends when the model has no more tool requests.

        for call in msg.tool_calls:
            args = json.loads(call.function.arguments)  # Model-supplied args CAN be wrong (failure mode #1).
            result = get_site_data(**args)  # Runs the requested local tool with those args.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result),
                }
            )


if __name__ == "__main__":
    brief = run("Is 48.06, -1.70 a good site for a 100MW data center? Use the data.")
    print(brief)
