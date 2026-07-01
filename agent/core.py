import json
import time
import re

from config import MODEL_PRIORITY
from agent.tools import tools_map, TOOLS_SCHEMA, selection_state, plot_route_map
from agent.prompts import SYSTEM_PROMPT
from agent.utils import get_client

# Convert OpenAI-style tools schema to Anthropic format
ANTHROPIC_TOOLS = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in TOOLS_SCHEMA
]

# Keywords that count as the user explicitly picking timetable vs frequency chart.
# A bare "schedule"/"מה הלוח זמנים" question matches neither, so it stays ambiguous.
_TIMETABLE_KEYWORDS = {
    "timetable", "departure time", "departure times", "exact time",
    "לוח זמנים", "זמני יציאה", "מתי יוצא", "מתי יוצאת", "מתי היציאות",
}
_FREQUENCY_KEYWORDS = {
    "frequency", "how often", "average departure", "per hour",
    "תדירות", "כל כמה", "בממוצע",
}
# Broader net: is this conversation about departures/schedule at all (regardless
# of whether timetable vs frequency has been specified yet)?
_SCHEDULE_KEYWORDS = _TIMETABLE_KEYWORDS | _FREQUENCY_KEYWORDS | {
    "schedule", "departure", "departures", "how many trips", "trips per",
    "לוח", "יציאות", "נסיעות",
}


def get_messages(history, provider) -> list:
    if isinstance(history, str):
        history = [{"role": "user", "content": history}]

    pending = selection_state.get("pending_type")

    system = SYSTEM_PROMPT
    if pending == "direction":
        directions = selection_state.get("directions", [])
        all_route_ids = selection_state.get("all_route_ids", [])
        dir_text = "\n".join(
            f"{d['option_number']}. {d['headsign']} (route_id={d['route_id']})"
            for d in directions
        )
        all_num = len(directions) + 1
        system += (
            f"\n\n⚠️ CURRENT STATE: You showed the user {len(directions)} directions and are waiting for their choice."
            f"\nDirections:\n{dir_text}"
            f"\n{all_num}. כל הכיוונים — route_ids={all_route_ids}"
            f"\nBased on the user's response, call get_line_stops with:"
            f"\n- A specific direction: get_line_stops(route_ids=[<that direction's route_id>])"
            f"\n- All directions: get_line_stops(route_ids={all_route_ids})"
            f"\nDo NOT call get_line_variants or get_line_directions again."
        )
    elif pending == "schedule_choice":
        route_ids = selection_state.get("schedule_route_ids", [])
        agency = selection_state.get("schedule_agency", "")
        line_num = selection_state.get("schedule_line_number", "")
        system += (
            f"\n\n⚠️ CURRENT STATE: Line {line_num} of {agency} is already identified - "
            f"route_ids={route_ids}. You already asked the user to choose between timetable "
            f"and frequency chart; their latest message is that choice.\n"
            f"- Timetable → call get_departure_timetable(route_ids={route_ids}, specific_day=...). "
            f"If they didn't name a day, ask which day first instead of calling the tool.\n"
            f"- Frequency chart → call get_departure_schedule(route_ids={route_ids}), then "
            f"plot_departure_schedule(route_ids={route_ids}).\n"
            f"Do NOT call get_line_variants again - the line is already resolved."
        )
    elif pending:
        options = selection_state.get("agencies", []) if pending == "agency" else [
            g["route_long_names"][0] if g.get("route_long_names") else ""
            for g in selection_state.get("grouped_lines", [])
        ]
        options_text = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
        system += (
            f"\n\n⚠️ CURRENT STATE: You showed a numbered list and are waiting for the user to choose."
            f"\nThe list was:\n{options_text}"
            f"\nThe user's latest message is either a number or a name from this list."
            f"\nFind the matching number and call select_option(option_number)."
            f"\nDo NOT call get_line_variants. Do NOT pass agency names as arguments."
        )

    last_user_msg = next(
        (m["content"] for m in reversed(history) if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "",
    )
    reply_language = "Hebrew" if re.search(r"[֐-׿]", last_user_msg) else "English"
    system += (
        f"\n\n⚠️ LANGUAGE: The user's last message is in {reply_language}. "
        f"Write your entire reply — including any explanatory sentences around a numbered "
        f"list — in {reply_language}. Only GTFS names (stops, agencies, headsigns, route "
        f"names) stay in their original Hebrew form."
    )

    if provider == "anthropic":
        return [m for m in history if m["role"] != "system"]
    return [{"role": "system", "content": system}] + list(history)


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


def _reasoning_kwargs(provider: str, model: str) -> dict:
    """
    Extra request kwargs to keep chain-of-thought out of message.content.
    Each provider/model family controls this differently:
    - Groq gpt-oss models: only accept include_reasoning (reasoning_format
      isn't supported for them).
    - Groq qwen3: accepts reasoning_format, and defaults to "raw" (reasoning
      inline in content via <think> tags) if omitted - a different knob
      entirely from gpt-oss's, and the one that was leaking here.
    - Google: reasoning_effort="none" disables Gemini's "thinking" pass,
      which also cuts hidden thinking-token cost.
    """
    if provider == "groq":
        if model.startswith("qwen"):
            return {"reasoning_format": "hidden"}
        return {"include_reasoning": False}
    if provider == "google":
        return {"reasoning_effort": "none"}
    return {}


def _call_llm(messages, provider, model, client, tool_choice="auto"):
    if provider == "anthropic":
        return client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=ANTHROPIC_TOOLS,
        )
    else:
        kwargs = dict(model=model, messages=messages, tools=TOOLS_SCHEMA, tool_choice=tool_choice)
        if provider != "google":
            kwargs["parallel_tool_calls"] = False
        kwargs.update(_reasoning_kwargs(provider, model))
        return client.chat.completions.create(**kwargs)


def _parse_response(response, provider):
    """
    Return (content_text, tool_calls_list) normalized across providers.
    tool_calls_list items have: .id, .function.name, .function.arguments (JSON string)
    """
    if provider == "anthropic":
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


def _append_tool_result(messages, tool_call_id, func_name, result, provider, anthropic_raw_response=None):
    """Add the assistant tool-call + tool result to message history."""
    if provider == "anthropic":
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


def extract_map_data(result: str) -> dict | None:
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("chart_type") == "route_map":
            return data
    except Exception:
        pass
    return None


def extract_chart_data(result: str) -> dict | None:
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("chart_type") == "departure_schedule":
            return data
    except Exception:
        pass
    return None


def extract_timetable_data(result: str) -> dict | None:
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("timetable_type") == "departure_timetable":
            return data
    except Exception:
        pass
    return None


def _summarize_tool_result(func_name: str, content: str) -> str:
    """Condense a tool result to a one-line summary for message history trimming."""
    if len(content) <= 300:
        return content
    try:
        data = json.loads(content)
        if func_name == "get_line_stops":
            dirs = data if isinstance(data, list) else [data]
            parts = [f"{d.get('headsign', '?')} ({d.get('stops_count', '?')} stops)" for d in dirs]
            return f"[get_line_stops: {len(dirs)} direction(s) — {'; '.join(parts)}]"
        elif func_name == "get_line_variants":
            return (f"[get_line_variants: line={data.get('line_number')}, "
                    f"agency={data.get('agency_name')}, "
                    f"can_proceed={data.get('can_proceed')}, "
                    f"clarification_needed={data.get('clarification_needed')!r}]")
        elif func_name == "select_option":
            return f"[select_option: can_proceed={data.get('can_proceed')}, agency={data.get('agency_name')}]"
        elif func_name == "run_sql":
            rows = data if isinstance(data, list) else []
            return f"[run_sql: {len(rows)} row(s) returned]"
        elif func_name == "get_departure_timetable":
            dirs = data.get("directions", {}) if isinstance(data, dict) else {}
            total = sum(len(v.get("departures", [])) for v in dirs.values())
            return f"[get_departure_timetable: {len(dirs)} direction(s), {total} departures on {data.get('day', '?')}]"
        elif func_name == "get_departure_schedule":
            routes = list(data.keys()) if isinstance(data, dict) else []
            day_types = list(list(data.values())[0].keys()) if routes else []
            return f"[get_departure_schedule: {len(routes)} route(s), day types: {', '.join(str(d) for d in day_types)}]"
        elif func_name == "plot_departure_schedule":
            return "[plot_departure_schedule: chart generated and sent to UI]"
        else:
            return f"[{func_name}: result summarized ({len(content)} chars)]"
    except (json.JSONDecodeError, TypeError):
        return f"[{func_name}: {content[:150]}...]"


def _trim_tool_results(messages: list, tool_call_names: dict) -> None:
    """Replace tool result content in-place with short summaries."""
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            func_name = tool_call_names.get(m.get("tool_call_id", ""), "unknown")
            content = m.get("content", "")
            if isinstance(content, str):
                m["content"] = _summarize_tool_result(func_name, content)
        elif m.get("role") == "user" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if block.get("type") == "tool_result":
                    func_name = tool_call_names.get(block.get("tool_use_id", ""), "unknown")
                    content = block.get("content", "")
                    if isinstance(content, str):
                        block["content"] = _summarize_tool_result(func_name, content)


def _detect_limit_type(error_text: str) -> str:
    """
    Best-effort classification of a rate-limit error's period, from its message text.
    Providers phrase this differently - e.g. Google's quotaId comes back as
    camelCase like "GenerateRequestsPerDayPerProjectPerModel-FreeTier" (no
    space around "Per"/"Day"), so match with an optional separator instead of
    a literal "per day" substring.
    """
    t = error_text.lower()
    if re.search(r"per.?day|rpd|tpd|daily", t):
        return "daily"
    if re.search(r"per.?hour|rph|hourly", t):
        return "hourly"
    if re.search(r"per.?minute|rpm|tpm", t):
        return "per-minute"
    if re.search(r"per.?second|rps|tps", t):
        return "per-second"
    if "high demand" in t or "unavailable" in t or "overloaded" in t:
        return "availability"
    return "rate"


def _is_hebrew(text: str) -> bool:
    return bool(re.search(r"[֐-׿]", text))


def _schedule_type_question(history: list) -> str:
    """Deterministic, code-built timetable-vs-frequency question, in the user's language."""
    last_user_msg = next(
        (m["content"] for m in reversed(history) if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "",
    )
    if _is_hebrew(last_user_msg):
        return (
            "אילו נתונים תרצה על הקו?\n"
            "1. לוח זמנים — זמני יציאה מדויקים ליום מסוים\n"
            "2. תרשים תדירות — ממוצע יציאות לשעה לפי סוג יום"
        )
    return (
        "Which would you like?\n"
        "1. Timetable — exact departure times for a specific day\n"
        "2. Frequency chart — average departures per hour by day type"
    )


_DAY_ALIASES = {
    "sunday": "sunday", "ראשון": "sunday",
    "monday": "monday", "שני": "monday",
    "tuesday": "tuesday", "שלישי": "tuesday",
    "wednesday": "wednesday", "רביעי": "wednesday",
    "thursday": "thursday", "חמישי": "thursday",
    "friday": "friday", "שישי": "friday",
    "saturday": "saturday", "שבת": "saturday",
}


def _extract_day(text: str):
    t = text.lower()
    for alias, day in _DAY_ALIASES.items():
        if alias in t:
            return day
    return None


def _summarize_frequency(question, line_num, agency, schedule_json, provider, model, client) -> str:
    """
    Small LLM pass over the REAL per-hour departure data, producing a short
    2-3 sentence summary (peak hours, general pattern) - never a table, since
    the chart already shows the numbers. Feeding it real data (rather than
    letting the main model answer from a stale/trimmed context) is what
    prevents the fabricated-looking frequency ranges seen before.
    """
    system = (
        "You are given real departure-frequency data (JSON: route_id -> day_type -> hour -> "
        "average departures) for a bus line. Write a SHORT 2-3 sentence summary, in the same "
        "language as the user's question, mentioning peak hours and the general pattern. "
        "Do NOT restate the data as a table or list. Do NOT invent numbers not in the data."
    )
    user = f"User's question: {question}\nLine {line_num} ({agency})\nData:\n{schedule_json}"
    try:
        if provider == "anthropic":
            resp = client.messages.create(
                model=model, max_tokens=400, system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        else:
            kwargs = dict(model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            kwargs.update(_reasoning_kwargs(provider, model))
            resp = client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
        return text.strip() or "Here is the frequency chart."
    except Exception:
        return "Here is the frequency chart."


def _is_retryable_error(error_text: str, status) -> bool:
    """
    True for rate-limit/quota errors AND transient server-side unavailability
    (e.g. Gemini's 503 "This model is currently experiencing high demand") -
    both are cases where falling through to the next model in MODEL_PRIORITY
    (or waiting and retrying) is the right move, rather than surfacing a raw
    error. A real 503 with this exact wording is what slipped through the
    original rate-limit-only check and crashed a whole request (status=503
    isn't 429, and "high demand" doesn't contain any rate-limit keyword).
    """
    t = error_text.lower()
    return (
        status in (413, 429, 503) or "rate_limit" in t or "too large" in t
        or "overloaded" in t or "quota" in t or "resource_exhausted" in t
        or "high demand" in t or "unavailable" in t
    )


def _verify_answer(question: str, draft: str, provider: str, model: str, client) -> str:
    """
    One cheap extra LLM pass: check the draft answer actually satisfies the
    user's question and is presented clearly, and revise only if needed.
    Deliberately bypasses _call_llm/TOOLS_SCHEMA (no tools needed here) to
    keep this pass small - it's a token cost added on top of every answer,
    so it should stay minimal.
    """
    system = (
        "You are reviewing a draft answer to a user's public-transport question. "
        "Check: (1) does it fully answer what was asked, (2) is it clear and "
        "user-friendly (lists for multiple items, stop codes included, no raw "
        "SQL/JSON). If it already satisfies both, output it completely unchanged. "
        "Otherwise, output a corrected version. Output ONLY the final answer text - "
        "no meta-commentary about the review."
    )
    user = f"User's question:\n{question}\n\nDraft answer:\n{draft}"
    try:
        if provider == "anthropic":
            resp = client.messages.create(
                model=model, max_tokens=1024, system=system,
                messages=[{"role": "user", "content": user}],
            )
            verified = "".join(b.text for b in resp.content if b.type == "text")
        else:
            kwargs = dict(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            kwargs.update(_reasoning_kwargs(provider, model))
            resp = client.chat.completions.create(**kwargs)
            verified = resp.choices[0].message.content or ""
        return verified.strip() or draft
    except Exception:
        return draft


def react_agent(question: str, context: list = None, max_steps: int = 15, stop_event=None):
    """
    question: the current user message (string)
    context:  previous conversation turns as [{"role": ..., "content": ...}, ...]
              pass None or [] for a fresh conversation

    Model selection is automatic: starts at MODEL_PRIORITY[0] and, on a
    rate-limit/quota error, silently advances to the next entry (logged as a
    "switch" step) rather than asking the user to pick a provider.
    """
    model_idx = 0
    provider, model = MODEL_PRIORITY[model_idx]
    client = get_client(provider)

    history = list(context or []) + [{"role": "user", "content": question}]
    print(f"[Agent] New query -> provider={provider!r} model={model!r}")
    messages = get_messages(history, provider)

    # If nothing in this conversation has named timetable/frequency yet, the model
    # must ask before jumping straight to one - enforced below rather than left to
    # prompt-following alone, since that's proven unreliable on weaker fallback
    # models (see get_line_variants' pending-selection guard for the same idea).
    # Checks both roles: either the user named it explicitly, or the assistant
    # already asked the 1/2 clarifying question earlier (its own phrasing includes
    # "timetable"/"frequency chart"), meaning a later bare "1"/"2" reply is answering it.
    all_text = " ".join(
        m.get("content", "") for m in history if isinstance(m.get("content"), str)
    ).lower()
    must_ask_schedule_type = not any(k in all_text for k in _TIMETABLE_KEYWORDS | _FREQUENCY_KEYWORDS)
    is_schedule_question = any(k in all_text for k in _SCHEDULE_KEYWORDS)

    # --- Deterministic resolution of a pending timetable-vs-frequency choice ---
    # Bypasses the LLM's tool-call decision entirely when the reply is
    # unambiguous, so a rate-limited/weaker fallback model can't mishandle it
    # (and it's faster - one fewer LLM round-trip for the common case).
    if selection_state.get("pending_type") == "schedule_choice":
        sched_route_ids = selection_state.get("schedule_route_ids", [])
        sched_agency = selection_state.get("schedule_agency", "")
        sched_line_num = selection_state.get("schedule_line_number", "")
        q_lower = question.lower().strip()
        # Match "1"/"2" as a standalone token anywhere in the reply, not just an
        # exact match - natural replies like "option 1, sunday please" don't
        # equal "1" but clearly mean option 1. Safe to be lenient here since this
        # only runs right after we asked the user to pick 1 or 2.
        wants_frequency = bool(re.search(r"\b2\b", q_lower)) or any(k in q_lower for k in _FREQUENCY_KEYWORDS)
        wants_timetable = bool(re.search(r"\b1\b", q_lower)) or any(k in q_lower for k in _TIMETABLE_KEYWORDS)

        if wants_frequency and sched_route_ids:
            selection_state.update({"pending_type": None, "schedule_route_ids": [], "schedule_agency": None, "schedule_line_number": None})
            sched_result = tools_map["get_departure_schedule"](route_ids=sched_route_ids)
            plot_result = tools_map["plot_departure_schedule"](route_ids=sched_route_ids)
            fast_log = [
                {"type": "action", "tool": "get_departure_schedule", "args": {"route_ids": sched_route_ids}, "observation": sched_result[:500]},
                {"type": "action", "tool": "plot_departure_schedule", "args": {"route_ids": sched_route_ids}, "observation": plot_result[:500]},
            ]
            cd = extract_chart_data(plot_result)
            yield {"status": "step", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": cd, "timetable_data": None, "answer": None}
            summary = _summarize_frequency(question, sched_line_num, sched_agency, sched_result, provider, model, client)
            yield {"status": "done", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": cd, "timetable_data": None, "answer": summary}
            return

        if wants_timetable and sched_route_ids:
            day = _extract_day(q_lower)
            if day:
                selection_state.update({"pending_type": None, "schedule_route_ids": [], "schedule_agency": None, "schedule_line_number": None})
                tt_result = tools_map["get_departure_timetable"](route_ids=sched_route_ids, specific_day=day)
                fast_log = [{"type": "action", "tool": "get_departure_timetable", "args": {"route_ids": sched_route_ids, "specific_day": day}, "observation": tt_result[:500]}]
                td = extract_timetable_data(tt_result)
                yield {"status": "step", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": None, "timetable_data": td, "answer": None}
                if _is_hebrew(question):
                    answer = f"הנה לוח הזמנים לקו {sched_line_num} ({sched_agency}) ליום {day}:"
                else:
                    answer = f"Here is the timetable for line {sched_line_num} ({sched_agency}) on {day.capitalize()}:"
                yield {"status": "done", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": None, "timetable_data": td, "answer": answer}
                return
            # no day mentioned yet - fall through to the normal LLM flow, which is
            # now informed via get_messages()'s schedule_choice branch and will ask for it

    log, coords = [], []
    map_data = None
    chart_data = None
    timetable_data = None
    tool_calls_made = 0
    MAX_OBS_CHARS = 2000
    current_response = None
    tool_call_names = {}  # call_id → func_name, used for trimming
    pending_plot_args = None  # set after get_departure_schedule until plot_departure_schedule runs
    data_tool_used = False  # set once a real answer-producing tool (stops/map/sql/schedule) has run
    for step in range(max_steps):

        # --- Trim previous tool results to summaries before next LLM call ---
        if step > 0:
            _trim_tool_results(messages, tool_call_names)

        # --- Check stop request ---
        if stop_event and stop_event.is_set():
            yield {"status": "done", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": "Stopped by user."}
            return

        # --- Call the LLM ---
        try:
            current_response = _call_llm(messages, provider, model, client)
        except Exception as e:
            es = str(e)
            status = getattr(e, "status_code", None)
            print(f"[Agent] LLM error - type={type(e).__name__!r} status={status!r} msg={es[:300]!r}")

            if "tool_use_failed" in es or (status == 400 and "tool" in es.lower()):
                log.append({"type": "retry", "text": "tool_use_failed - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    "Your previous tool call was malformed. Use the structured "
                    "function-calling format. Try again."})
                continue

            if _is_retryable_error(es, status):
                limit_type = _detect_limit_type(es)

                if model_idx + 1 < len(MODEL_PRIORITY):
                    # --- Fall through to the next model in the priority chain ---
                    old_model = model
                    model_idx += 1
                    provider, model = MODEL_PRIORITY[model_idx]
                    client = get_client(provider)
                    log.append({
                        "type": "switch",
                        "text": f"{limit_type} limit on {old_model} - switching to {model}",
                        "from_model": old_model,
                        "to_model": model,
                        "limit_type": limit_type,
                    })
                    yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                    continue

                # --- Whole chain exhausted: wait and retry the last model ---
                wait_s = 60 if provider == "google" else 20
                log.append({
                    "type": "retry",
                    "text": f"rate limit ({limit_type}) for {model} - waiting {wait_s}s",
                    "model": model,
                    "limit_type": limit_type,
                    "wait_s": wait_s,
                })
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                for _ in range(wait_s * 2):  # 0.5s steps, interruptible
                    if stop_event and stop_event.is_set():
                        break
                    time.sleep(0.5)
                continue

            raise

        content, tool_calls = _parse_response(current_response, provider)

        # --- No tool call: final answer ---
        if not tool_calls:
            if '"type": "function"' in content or ('"name":' in content and '"arguments":' in content):
                log.append({"type": "retry", "text": "model emitted tool call as text - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    "You wrote a tool call as plain text. Use the real function-calling "
                    "mechanism, or give your final answer in plain language."})
                continue

            if must_ask_schedule_type and is_schedule_question and not data_tool_used:
                # The model gave a final answer (real or fabricated) without ever
                # calling a schedule tool and without the timetable/frequency choice
                # being resolved - override it with the deterministic question rather
                # than risk shipping a hallucinated answer built from no real data.
                answer = _schedule_type_question(history)
                yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": answer}
                return

            if pending_plot_args is not None:
                log.append({"type": "retry", "text": "must call plot_departure_schedule before finishing - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    f"You called get_departure_schedule but haven't called plot_departure_schedule yet. "
                    f"Call plot_departure_schedule(route_ids={pending_plot_args['route_ids']}, "
                    f"specific_day={pending_plot_args['specific_day']!r}) now, then give your final answer."})
                continue

            if tool_calls_made == 0:
                # Only force a tool call if the question is about transport data
                transport_keywords = {"line", "stop", "route", "bus", "operator", "agency", "קו", "תחנה", "מפעיל"}
                first_user_msg = next(
                    (m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)),
                    ""
                )
                is_transport = any(kw in first_user_msg.lower() for kw in transport_keywords)
                if is_transport:
                    log.append({"type": "retry", "text": "model answered without calling any tool - retrying"})
                    yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                    messages.append({"role": "user", "content":
                        "You have NOT called any tool yet. "
                        "For questions about a specific line number, call get_line_variants() first. "
                        "For general database questions, call run_sql() directly."})
                    continue

            coords += extract_coords(content)
            log.append({"type": "verify", "text": "checking the answer against the question"})
            yield {"status": "step", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
            content = _verify_answer(question, content, provider, model, client)
            yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": content}
            return

        # --- Tool calls: execute and feed results back ---
        # For OpenAI/Groq, append the assistant message now
        if provider != "anthropic":
            messages.append(current_response.choices[0].message)

        stop_after_tool = False
        can_proceed = False
        last_parsed = {}

        for tool_call in tool_calls:
            func_name = tool_call.function.name
            tool_call_names[tool_call.id] = func_name
            args = json.loads(tool_call.function.arguments) or {}

            if func_name == "get_line_variants":
                args = {k: v for k, v in args.items() if k in {"line_number", "agency_name"}}

            if must_ask_schedule_type and func_name in ("get_departure_timetable", "get_departure_schedule"):
                # Code-built, deterministic clarifying question instead of feeding back
                # a corrective tool error and trusting the model to phrase a short,
                # on-template reply - that round trip has drifted into long, unrelated
                # "what do you mean?" answers on some fallback models.
                answer = _schedule_type_question(history)
                yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": answer}
                return

            yield {"status": "calling", "tool": func_name, "args": args,
                   "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}

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

            if func_name in ("get_line_stops", "run_sql", "get_departure_timetable",
                              "get_departure_schedule", "plot_departure_schedule"):
                # Some real data tool answered the question - the schedule-type
                # guard below should only fire when NOTHING has answered it yet.
                data_tool_used = True

            if func_name == "get_departure_schedule":
                pending_plot_args = {"route_ids": args.get("route_ids"), "specific_day": args.get("specific_day")}

            if func_name == "plot_departure_schedule":
                pending_plot_args = None
                cd = extract_chart_data(result)
                if cd:
                    chart_data = cd

            if func_name == "get_departure_timetable":
                td = extract_timetable_data(result)
                if td:
                    timetable_data = td

            log.append({"type": "action", "tool": func_name, "args": args, "observation": result[:500]})
            coords += extract_coords(result)
            yield {"status": "step", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}

            trimmed = result if len(result) <= MAX_OBS_CHARS else result[:MAX_OBS_CHARS] + "\n...[truncated]"
            _append_tool_result(messages, tool_call.id, func_name, trimmed, provider, current_response)

            if stop_after_tool or can_proceed:
                break

        # --- Line identified: inject route_ids and let loop continue ---
        if can_proceed:
            selected = last_parsed.get("selected_line", {})
            route_ids = selected.get("route_ids", [])
            agency = last_parsed.get("agency_name") or selected.get("agency_name", "")
            line_num = last_parsed.get("line_number", "")
            ids_str = ", ".join(str(r) for r in route_ids)

            # Automatically plot the route map the moment a line is resolved -
            # deterministic, not something the model decides to call, so a map
            # reliably accompanies every answer about a specific line regardless
            # of what the model does next for the text answer.
            map_result = plot_route_map(route_ids, line_num=line_num, agency=agency)
            md = extract_map_data(map_result)
            if md:
                map_data = md
                log.append({"type": "action", "tool": "plot_route_map", "args": {"route_ids": route_ids}, "observation": map_result[:500]})
                yield {"status": "step", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}

            if must_ask_schedule_type and is_schedule_question:
                # Persist the resolved line so the NEXT turn (the user's timetable-vs-
                # frequency reply) doesn't have to re-derive it - chat history across
                # turns only keeps rendered text, not these route_ids, which is exactly
                # what caused the model to re-run get_line_variants from scratch before.
                selection_state.update({
                    "pending_type": "schedule_choice",
                    "schedule_route_ids": route_ids,
                    "schedule_agency": agency,
                    "schedule_line_number": line_num,
                })

            messages.append({
                "role": "user",
                "content": (
                    f"Line {line_num} of {agency} is now uniquely identified. "
                    f"route_ids = {route_ids}. "
                    f"These route_ids are the same line in different directions — always include all of them.\n"
                    f"A route map (stops numbered, one color per direction) has already been displayed to the "
                    f"user automatically — do NOT call any map tool, it doesn't exist. You may mention the map "
                    f"briefly in your answer if relevant.\n"
                    f"Based on the user's original question, decide what to do next:\n"
                    f"• Stop questions → call get_line_directions(route_ids={route_ids})\n"
                    f"• Schedule / departure / timetable questions → DO NOT call any tool yet. First ask the user to choose: (1) Timetable — exact times for a specific day, or (2) Frequency chart — average departures per hour by day type. Wait for their answer before proceeding.\n"
                    f"• Other questions → use run_sql() with WHERE route_id IN ({ids_str})"
                ),
            })
            can_proceed = False
            continue

        # --- Clarification needed: Python builds the numbered list, LLM writes the response ---
        if stop_after_tool:
            formatted_list = ""
            try:
                options = last_parsed.get("options", [])
                n = len(options)
                clarification_type = last_parsed.get("clarification_needed", "")
                formatted_list = "\n".join(f"{opt['option_number']}. {opt['label']}" for opt in options)

                if clarification_type == "direction":
                    after_list = (
                        f"Ask the user to enter a number from 1 to {n-1} for a specific direction, "
                        f"or {n} for all directions."
                    )
                elif clarification_type == "agency":
                    after_list = (
                        f"If the question is purely about who operates the line, this list IS the answer. "
                        f"Otherwise ask the user to enter a number from 1 to {n}."
                    )
                else:
                    after_list = f"Ask the user to enter a number from 1 to {n}."

                messages.append({
                    "role": "user",
                    "content": (
                        f"Include this numbered list verbatim in your response:\n\n"
                        f"{formatted_list}\n\n"
                        f"Do not reformat, renumber, or remove any item. "
                        f"{after_list}"
                    ),
                })
            except Exception:
                pass

            # Same rate-limit/model-fallback handling as the main loop above -
            # this call has no tool_calls step afterwards to retry from, so a
            # crash here used to skip straight past every log entry collected
            # so far and surface a raw provider error as the final answer.
            content = formatted_list or "Sorry, I couldn't reach any model right now — please try again in a moment."
            while True:
                try:
                    llm_resp = _call_llm(messages, provider, model, client, tool_choice="none")
                    content, _ = _parse_response(llm_resp, provider)
                    break
                except Exception as e:
                    es = str(e)
                    status = getattr(e, "status_code", None)
                    print(f"[Agent] LLM error (finalizing clarification) - type={type(e).__name__!r} status={status!r} msg={es[:300]!r}")
                    if not _is_retryable_error(es, status) or model_idx + 1 >= len(MODEL_PRIORITY):
                        break
                    limit_type = _detect_limit_type(es)
                    old_model = model
                    model_idx += 1
                    provider, model = MODEL_PRIORITY[model_idx]
                    client = get_client(provider)
                    log.append({
                        "type": "switch",
                        "text": f"{limit_type} limit on {old_model} - switching to {model}",
                        "from_model": old_model,
                        "to_model": model,
                        "limit_type": limit_type,
                    })
                    yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}

            yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": content}
            return

    yield {"status": "done", "log": list(log), "coords": list(coords), "chart_data": chart_data, "answer": "Max steps reached"}
