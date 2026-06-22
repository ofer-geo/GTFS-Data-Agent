import json
import time
import re

from groq import BadRequestError, APIStatusError

from config import MODEL
from agent.tools import tools_map, TOOLS_SCHEMA
from agent.prompts import SYSTEM_PROMPT
from agent.utils import get_client

# Create the LLM client once when the module loads
client = get_client()


def get_messages(history) -> list:
    """
    Build the full message list: system prompt + conversation history.
    history can be a plain string (single question) or a list of
    {"role": "user"/"assistant", "content": "..."} dicts.
    """
    if isinstance(history, str):
        history = [{"role": "user", "content": history}]
    return [{"role": "system", "content": SYSTEM_PROMPT}] + history


def extract_coords(text: str) -> list:
    """
    Parse {lat, lon, label} objects from the agent's text response.
    Used to extract map coordinates from the final answer or tool results.
    Only keeps coordinates that fall within Israel's bounding box.
    """
    coords = []
    for block in re.findall(r'\{[^{}]+\}', text):
        clean = block.replace('\\"', '"').replace("\\", "")
        lat_m = re.search(r'"?lat"?\s*:\s*(-?\d{1,2}\.\d+)', clean)
        lon_m = re.search(r'"?lon"?\s*:\s*(-?\d{1,3}\.\d+)', clean)
        if not (lat_m and lon_m):
            continue
        lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        # Sanity check: only accept coordinates inside Israel
        if not (29 < lat < 34 and 34 < lon < 36):
            continue
        label_m = re.search(r'"?label"?\s*:\s*"?([^"}]+)', clean)
        label = label_m.group(1).strip().strip('"') if label_m else ""
        coords.append({"lat": lat, "lon": lon, "label": label})

    # Remove duplicate coordinates
    seen, out = set(), []
    for c in coords:
        key = (round(c["lat"], 5), round(c["lon"], 5))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def react_agent(history, max_steps: int = 15):
    """
    Run the ReAct loop for a given question.

    This is a generator — it yields a status dict after every step so
    app.py can update the UI in real time. Each yielded dict contains:
      - status: "calling" | "step" | "retry" | "done"
      - log: list of completed steps so far
      - coords: list of {lat, lon, label} found so far
      - answer: the final answer string (only when status == "done")
    """
    messages = get_messages(history)
    log, coords = [], []
    tool_calls_made = 0
    # Truncate long tool results so we don't overflow the LLM context window
    MAX_OBS_CHARS = 3000

    for step in range(max_steps):

        # --- Call the LLM ---
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS_SCHEMA,
                tool_choice="auto",
                parallel_tool_calls=False,  # one tool at a time, as per system prompt rule 1
            )
        except BadRequestError as e:
            # The model tried to call a tool but formatted it incorrectly
            if "tool_use_failed" in str(e):
                log.append({"type": "retry", "text": "tool_use_failed - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                messages.append({"role": "user", "content":
                    "Your previous tool call was malformed. Call exactly ONE tool using the "
                    "structured function-calling format, NOT as plain text. Try again."})
                continue
            raise
        except APIStatusError as e:
            # Rate limit or context too large — wait and retry
            es = str(e)
            if getattr(e, "status_code", None) in (413, 429) or "rate_limit" in es or "too large" in es.lower():
                log.append({"type": "retry", "text": "rate limit - waiting 20s"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                time.sleep(20)
                continue
            raise

        msg = response.choices[0].message

        # --- No tool call: the agent is ready to give a final answer ---
        if not msg.tool_calls:
            content = msg.content or ""

            # Guard: sometimes the model writes a tool call as plain text instead of
            # using the actual function-calling mechanism — catch and correct that
            if '"type": "function"' in content or ('"name":' in content and '"arguments":' in content):
                log.append({"type": "retry", "text": "model emitted tool call as text - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                messages.append({"role": "user", "content":
                    "You wrote a tool call as plain text. That does NOT work. Make a REAL function "
                    "call using the API's tool-calling mechanism, OR if you already have the data, "
                    "give the final answer in plain language with a coordinate list."})
                continue

            # Guard: if the model tries to answer without ever calling a tool, force it to try
            if tool_calls_made == 0:
                log.append({"type": "retry", "text": "model answered without calling any tool - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                messages.append({"role": "user", "content":
                    "You have NOT called any tool yet. Call run_sql() or get_schema() "
                    "to look up the answer — do not answer from memory."})
                continue

            # Extract any map coordinates from the final answer
            coords += extract_coords(content)
            yield {"status": "done", "log": list(log), "coords": list(coords), "answer": content}
            return

        # --- Tool call: execute it and feed the result back to the LLM ---
        messages.append(msg)  # add the assistant's tool-call message to history

        for tool_call in msg.tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments) or {}

            # Let app.py show "calling tool X..." in the UI
            yield {"status": "calling", "tool": func_name, "args": args,
                   "log": list(log), "coords": list(coords), "answer": None}

            # Execute the tool
            if func_name not in tools_map:
                result = f"Error: tool '{func_name}' does not exist."
            else:
                result = tools_map[func_name](**args)
                tool_calls_made += 1

            # Log this step and extract any coordinates from the tool result
            log.append({"type": "action", "tool": func_name, "args": args, "observation": result[:500]})
            coords += extract_coords(result)
            yield {"status": "step", "log": list(log), "coords": list(coords), "answer": None}

            # Truncate and add the tool result to message history for the next LLM call
            trimmed = result if len(result) <= MAX_OBS_CHARS else result[:MAX_OBS_CHARS] + "\n...[truncated]"
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": trimmed})

    # If we reach here the agent ran out of steps without a final answer
    yield {"status": "done", "log": list(log), "coords": list(coords), "answer": "Max steps reached"}