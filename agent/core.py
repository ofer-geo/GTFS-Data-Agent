import json
import time
import re

from config import PROVIDER, MODEL
from agent.tools import tools_map, TOOLS_SCHEMA
from agent.prompts import SYSTEM_PROMPT
from agent.utils import get_client

client = get_client()
print(f"[Agent] Provider: {PROVIDER!r}  Model: {MODEL!r}")

# Convert OpenAI-style tools schema to Anthropic format
ANTHROPIC_TOOLS = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in TOOLS_SCHEMA
]


def get_messages(history) -> list:
    if isinstance(history, str):
        history = [{"role": "user", "content": history}]

    from agent.tools import selection_state
    pending = selection_state.get("pending_type")

    system = SYSTEM_PROMPT
    if pending:
        system += (
            f"\n\n⚠️ CURRENT STATE: You presented a numbered list and are waiting for the user "
            f"to choose (pending_type='{pending}'). "
            f"The user's latest message is their selection number. "
            f"You MUST call select_option(option_number) with that number. "
            f"Do NOT call get_line_variants again."
        )

    if PROVIDER == "anthropic":
        return [m for m in history if m["role"] != "system"]
    return [{"role": "system", "content": system}] + list(history)


def _is_hebrew(text: str) -> bool:
    return any("א" <= c <= "ת" for c in text)


def extract_coords(text: str) -> list:
    coords = []
    for block in re.findall(r'\{[^{}]+\}', text):
        clean = block.replace('\\"', '"').replace("\\", "")
        lat_m = re.search(r'"?lat"?\s*:\s*(-?\d{1,2}\.\d+)', clean)
        lon_m = re.search(r'"?lon"?\s*:\s*(-?\d{1,3}\.\d+)', clean)
        if not (lat_m and lon_m):
            continue
        lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        if not (29 < lat < 34 and 34 < lon < 36):
            continue
        label_m = re.search(r'"?label"?\s*:\s*"?([^"}]+)', clean)
        label = label_m.group(1).strip().strip('"') if label_m else ""
        coords.append({"lat": lat, "lon": lon, "label": label})
    seen, out = set(), []
    for c in coords:
        key = (round(c["lat"], 5), round(c["lon"], 5))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _call_llm(messages, tool_choice="auto"):
    """Call the LLM using the correct SDK for the active provider."""
    if PROVIDER == "anthropic":
        return client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=ANTHROPIC_TOOLS,
        )
    else:
        return client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice=tool_choice,
            parallel_tool_calls=False,
        )


def _parse_response(response):
    """
    Return (content_text, tool_calls_list) normalized across providers.
    tool_calls_list items have: .id, .function.name, .function.arguments (JSON string)
    """
    if PROVIDER == "anthropic":
        text = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                class _TC:
                    pass
                class _Fn:
                    pass
                tc = _TC()
                fn = _Fn()
                fn.name = block.name
                fn.arguments = json.dumps(block.input)
                tc.id = block.id
                tc.function = fn
                tool_calls.append(tc)
        return text, tool_calls
    else:
        msg = response.choices[0].message
        return msg.content or "", msg.tool_calls or []


def _append_tool_result(messages, tool_call_id, func_name, result, anthropic_raw_response=None):
    """Add the assistant tool-call + tool result to message history."""
    if PROVIDER == "anthropic":
        # For Anthropic, the assistant turn must include the original content blocks
        if anthropic_raw_response and not any(
            isinstance(m.get("content"), list) for m in messages if m["role"] == "assistant"
        ):
            messages.append({"role": "assistant", "content": anthropic_raw_response.content})
        # Tool results go as a user message with tool_result blocks
        # Group multiple results under one user message
        last = messages[-1] if messages else {}
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            last["content"].append({"type": "tool_result", "tool_use_id": tool_call_id, "content": result})
        else:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_call_id, "content": result}
            ]})
    else:
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": result})


def react_agent(question: str, context: list = None, max_steps: int = 15, stop_event=None):
    """
    question: the current user message (string)
    context:  previous conversation turns as [{"role": ..., "content": ...}, ...]
              pass None or [] for a fresh conversation
    """
    history = list(context or []) + [{"role": "user", "content": question}]
    """
    ReAct loop. Yields status dicts so app.py can update the UI in real time.
    Accepts either a plain question string or a full conversation history list.
    """
    print(f"[Agent] New query → provider={PROVIDER!r} model={MODEL!r}")
    messages = get_messages(history)
    log, coords = [], []
    tool_calls_made = 0
    MAX_OBS_CHARS = 3000
    current_response = None
    for step in range(max_steps):

        # --- Check stop request ---
        if stop_event and stop_event.is_set():
            yield {"status": "done", "log": list(log), "coords": list(coords), "answer": "Stopped by user."}
            return

        # --- Call the LLM ---
        try:
            current_response = _call_llm(messages)
        except Exception as e:
            es = str(e)
            status = getattr(e, "status_code", None)
            print(f"[Agent] LLM error — type={type(e).__name__!r} status={status!r} msg={es[:300]!r}")

            if "tool_use_failed" in es or (status == 400 and "tool" in es.lower()):
                log.append({"type": "retry", "text": "tool_use_failed - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                messages.append({"role": "user", "content":
                    "Your previous tool call was malformed. Use the structured "
                    "function-calling format. Try again."})
                continue

            if status in (413, 429) or "rate_limit" in es or "too large" in es.lower() or "overloaded" in es.lower():
                log.append({"type": "retry", "text": "rate limit - waiting 20s"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                for _ in range(40):  # 40 × 0.5s = 20s, interruptible
                    if stop_event and stop_event.is_set():
                        break
                    time.sleep(0.5)
                continue

            raise

        content, tool_calls = _parse_response(current_response)

        # --- No tool call: final answer ---
        if not tool_calls:
            if '"type": "function"' in content or ('"name":' in content and '"arguments":' in content):
                log.append({"type": "retry", "text": "model emitted tool call as text - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                messages.append({"role": "user", "content":
                    "You wrote a tool call as plain text. Use the real function-calling "
                    "mechanism, or give your final answer in plain language."})
                continue

            if tool_calls_made == 0:
                log.append({"type": "retry", "text": "model answered without calling any tool - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "answer": None}
                messages.append({"role": "user", "content":
                    "You have NOT called any tool yet. First call run_sql() to query the "
                    "database, then present your findings to the user."})
                continue

            coords += extract_coords(content)
            yield {"status": "done", "log": list(log), "coords": list(coords), "answer": content}
            return

        # --- Tool calls: execute and feed results back ---
        # For OpenAI/Groq, append the assistant message now
        if PROVIDER != "anthropic":
            messages.append(current_response.choices[0].message)

        stop_after_tool = False
        can_proceed = False
        last_parsed = {}

        for tool_call in tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments) or {}

            if func_name == "get_line_variants":
                args = {k: v for k, v in args.items() if k in {"line_number", "agency_name"}}

            yield {"status": "calling", "tool": func_name, "args": args,
                   "log": list(log), "coords": list(coords), "answer": None}

            if func_name not in tools_map:
                result = f"Error: tool '{func_name}' does not exist."
            else:
                result = tools_map[func_name](**args)
                tool_calls_made += 1

            try:
                last_parsed = json.loads(result)
                if last_parsed.get("clarification_needed"):
                    stop_after_tool = True
                elif last_parsed.get("can_proceed"):
                    can_proceed = True
            except (json.JSONDecodeError, AttributeError):
                pass

            log.append({"type": "action", "tool": func_name, "args": args, "observation": result[:500]})
            coords += extract_coords(result)
            yield {"status": "step", "log": list(log), "coords": list(coords), "answer": None}

            trimmed = result if len(result) <= MAX_OBS_CHARS else result[:MAX_OBS_CHARS] + "\n...[truncated]"
            _append_tool_result(messages, tool_call.id, func_name, trimmed, current_response)

            if stop_after_tool or can_proceed:
                break

        # --- Line identified: say what was found and what can't be answered yet ---
        if can_proceed:
            selected = last_parsed.get("selected_line", {})
            agency = last_parsed.get("agency_name") or selected.get("agency_name", "")
            line_num = last_parsed.get("line_number", "")
            route_names = selected.get("route_long_names", [])
            route_desc = route_names[0] if route_names else selected.get("route_long_name", "")

            original_q = next(
                (m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)),
                ""
            )

            hebrew = _is_hebrew(original_q)
            if hebrew:
                answer = f"זיהיתי: קו {line_num} של {agency}"
                if route_desc:
                    answer += f" — {route_desc}"
                answer += f".\n\nאין לי עדיין כלי לענות על: \"{original_q}\"."
            else:
                answer = f"Identified: line {line_num} operated by {agency}"
                if route_desc:
                    answer += f" — {route_desc}"
                answer += f'.\n\nI don\'t yet have the tool to answer: "{original_q}".'

            yield {"status": "done", "log": list(log), "coords": list(coords), "answer": answer}
            return

        # --- Clarification needed: format numbered list directly in Python ---
        if stop_after_tool:
            try:
                options = last_parsed.get("options", [])
                ctype = last_parsed.get("clarification_needed", "")
                line_num = last_parsed.get("line_number", "")
                agency = last_parsed.get("agency_name", "")

                original_q = next(
                    (m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)),
                    ""
                )
                hebrew = _is_hebrew(original_q)

                if ctype == "agency":
                    intro = f"קו {line_num} קיים אצל מספר מפעילים. בחר מפעיל:" if hebrew else f"Line {line_num} is operated by multiple agencies. Choose one:"
                else:
                    intro = f"קו {line_num} של {agency} קיים במספר מסלולים. בחר מסלול:" if hebrew else f"Line {line_num} ({agency}) has multiple routes. Choose one:"

                lines = [intro, ""]
                for opt in options:
                    lines.append(f"{opt['option_number']}. {opt['label']}")
                n = len(options)
                lines.append(f"\nהזן מספר בין 1 ל-{n}." if hebrew else f"\nEnter a number between 1 and {n}.")
                answer = "\n".join(lines)
            except Exception:
                answer = "Please choose an option from the list above."

            yield {"status": "done", "log": list(log), "coords": list(coords), "answer": answer}
            return

    yield {"status": "done", "log": list(log), "coords": list(coords), "answer": "Max steps reached"}
